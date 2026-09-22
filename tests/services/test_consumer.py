from threading import Event
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.services import consumer as worker
from tests.services.test_sync_service import make_config


@pytest.mark.parametrize("failure", [None, "initialize", "lost_lock"])
def test_worker_releases_resources_on_shutdown_or_failure(tmp_path, monkeypatch, failure):
    config = make_config(tmp_path)
    queue, owner, lease = Mock(), Mock(), Mock()
    owner.acquire_run_lock.return_value = "owned-token"
    monkeypatch.setattr(worker, "TaskQueue", lambda _: queue)
    monkeypatch.setattr(worker, "RedisStateStore", lambda *args, **kwargs: owner)
    monkeypatch.setattr(worker, "RunLease", lambda *args: lease)
    stop = Event()
    build = Mock()
    if failure == "initialize":
        build.side_effect = ConnectionError("初始化失败")
    monkeypatch.setattr(worker, "build_service", build)
    guard = Mock()
    if failure == "lost_lock":
        guard.side_effect = RuntimeError("锁已失效")
    monkeypatch.setattr(worker, "assert_run_lock", guard)

    def finish_and_stop(*args):
        stop.set()
        return False

    process = Mock(side_effect=finish_and_stop)
    monkeypatch.setattr(worker, "process_one", process)
    if failure:
        with pytest.raises((ConnectionError, RuntimeError)):
            worker.consume_session(config, stop)
    else:
        worker.consume_session(config, stop)
    if failure:
        process.assert_not_called()
    else:
        process.assert_called_once()
    lease.close.assert_called_once()
    owner.release_run_lock.assert_called_once_with("owned-token")
    owner.close.assert_called_once()
    queue.close.assert_called_once()


def test_consumer_recovers_after_connection_failure(tmp_path, monkeypatch):
    stop = Event()
    config = make_config(tmp_path)
    from dataclasses import replace
    config = replace(config, queue_retry_delay_seconds=0)
    attempts = []

    def session(config, event):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("连接中断")
        event.set()

    monkeypatch.setattr(worker, "consume_session", session)
    worker.consume(config, stop)
    assert len(attempts) == 2

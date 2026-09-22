from unittest.mock import Mock

import pytest

from stocking_sheet_sync.entrypoints import worker
from tests.services.test_sync_service import make_config


@pytest.mark.parametrize("failure", [None, "initialize", "lost_lock"])
def test_worker_releases_resources_on_shutdown_or_failure(tmp_path, monkeypatch, failure):
    config = make_config(tmp_path)
    queue, owner, lease = Mock(), Mock(), Mock()
    owner.acquire_run_lock.return_value = "owned-token"
    monkeypatch.setattr(worker, "load_config", lambda: config)
    monkeypatch.setattr(worker, "TaskQueue", lambda _: queue)
    monkeypatch.setattr(worker, "RedisStateStore", lambda *args, **kwargs: owner)
    monkeypatch.setattr(worker, "RunLease", lambda *args: lease)
    handlers = {}

    def register(sig, handler):
        previous = handlers.get(sig)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(worker.signal, "signal", register)
    build = Mock()
    if failure == "initialize":
        build.side_effect = ConnectionError("初始化失败")
    monkeypatch.setattr(worker, "build_service", build)
    guard = Mock()
    if failure == "lost_lock":
        guard.side_effect = RuntimeError("锁已失效")
    monkeypatch.setattr(worker, "assert_run_lock", guard)

    def finish_and_stop(*args):
        handlers[worker.signal.SIGTERM](None, None)
        return False

    process = Mock(side_effect=finish_and_stop)
    monkeypatch.setattr(worker, "process_one", process)
    assert worker.run([]) == (1 if failure else 0)
    if failure:
        process.assert_not_called()
    else:
        process.assert_called_once()
    lease.close.assert_called_once()
    owner.release_run_lock.assert_called_once_with("owned-token")
    owner.close.assert_called_once()
    queue.close.assert_called_once()
    assert all(value is None for value in handlers.values())

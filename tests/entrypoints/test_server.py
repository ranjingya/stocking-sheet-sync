from threading import Event
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.entrypoints import server
from tests.services.test_sync_service import make_config


def test_server_accepts_webhook_while_consumer_is_busy_and_stops(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    queue = Mock()
    queue.enqueue.return_value = "1-0"
    monkeypatch.setattr(server, "load_config", lambda: config)
    monkeypatch.setattr(server, "TaskQueue", lambda _: queue)
    handlers = {}

    def register(sig, handler):
        previous = handlers.get(sig)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr(server.signal, "signal", register)
    ready, done = Event(), Event()

    def consume(config, stop):
        ready.set()
        stop.wait(5)
        done.set()

    monkeypatch.setattr(server, "consume", consume)
    http = Mock()

    def create(app, **kwargs):
        def run():
            assert ready.wait(2)
            assert not done.is_set()
            response = app.test_client().post(
                "/webhooks/base-record", json={"record_id": "rec_test"},
                headers={"Authorization": f"Bearer {config.webhook_secret}"},
            )
            assert response.status_code == 202
            assert not done.is_set()
            handlers[server.signal.SIGTERM](None, None)
        http.run.side_effect = run
        return http

    monkeypatch.setattr(server, "create_server", create)
    assert server.run([]) == 0
    assert done.is_set()
    http.close.assert_called_once()
    queue.close.assert_called_once()
    assert all(value is None for value in handlers.values())


def test_bind_failure_does_not_start_background_consumer(tmp_path, monkeypatch):
    queue, consume = Mock(), Mock()
    monkeypatch.setattr(server, "load_config", lambda: make_config(tmp_path))
    monkeypatch.setattr(server, "TaskQueue", lambda _: queue)
    monkeypatch.setattr(server, "consume", consume)
    monkeypatch.setattr(server, "create_server", Mock(side_effect=OSError("端口被占用")))
    with pytest.raises(OSError):
        server.run([])
    consume.assert_not_called()
    queue.close.assert_called_once()

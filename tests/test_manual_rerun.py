import json
from dataclasses import replace

import pytest

from stocking_sheet_sync.entrypoints import rerun as manual_rerun
from stocking_sheet_sync.services.sync import SyncService
from tests.test_sync_service import make_service


@pytest.fixture
def environment(tmp_path, monkeypatch):
    service, client, redis, clock = make_service(tmp_path)
    config = replace(service.config, webhook_secret="")
    closed = []
    monkeypatch.setattr(manual_rerun, "load_config", lambda: config)
    monkeypatch.setattr(manual_rerun, "RedisStateStore", lambda *a, **k: service.store)
    monkeypatch.setattr(service.store, "close", lambda: closed.append("store"))

    class Client:
        def __init__(self, cfg, app_id, secret, name):
            self.name = name

        def __getattr__(self, name):
            return getattr(client, name)

        def close(self):
            closed.append(self.name)

    monkeypatch.setattr(manual_rerun, "FeishuClient", Client)
    return service, client, redis, closed


def test_command_creates_new_batches_and_can_resume_without_server(environment, capsys, caplog):
    service, client, redis, closed = environment
    caplog.set_level("INFO")
    assert manual_rerun.run(["--record-id", "rec_test"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["force"] and first["request_id"]
    assert first["result"] == "copied"
    assert first["request_id"] in caplog.text
    assert "重试本批次命令" in caplog.text
    assert manual_rerun.run(["--record-id", "rec_test", "--request-id", first["request_id"]]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["result"] == "unchanged" and again["target_url"] == first["target_url"]
    assert manual_rerun.run(["--record-id", "rec_test"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["request_id"] != first["request_id"]
    assert client.copy_count == 2
    assert closed == ["message", "data", "store"] * 3


def test_command_busy_is_distinct_from_failure_and_preserves_resources(environment):
    service, client, redis, closed = environment
    service.store.acquire_run_lock(300)
    assert manual_rerun.run(["--record-id", "rec_test", "--request-id", "busy-id"]) == 3
    assert client.copy_count == 0
    assert closed == ["message", "data", "store"]


def test_command_copy_failure_returns_nonzero(environment, capsys):
    service, client, redis, closed = environment
    client.copy_error = "明确拒绝"
    assert manual_rerun.run(["--record-id", "rec_test", "--request-id", "failed-id"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "failed"
    assert closed == ["message", "data", "store"]


def test_command_fill_failure_preserves_copy_and_returns_success(environment, monkeypatch, capsys):
    service, client, redis, closed = environment

    def factory(cfg, data, message, store):
        return SyncService(
            replace(cfg, fill_history_enabled=True),
            data,
            message,
            store,
            history_filler=lambda *a: {"status": "needs_review", "reason": "填充失败"},
        )

    monkeypatch.setattr(manual_rerun, "SyncService", factory)
    assert manual_rerun.run(["--record-id", "rec_test"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "copied" and result["fill_degraded"]
    assert client.copy_count == 1
    assert closed == ["message", "data", "store"]


def test_command_partial_initialization_closes_created_resources(environment, monkeypatch):
    service, client, redis, closed = environment

    def fail(*a):
        raise RuntimeError("初始化失败")

    monkeypatch.setattr(manual_rerun, "FeishuClient", fail)
    assert manual_rerun.run(["--record-id", "rec_test"]) == 1
    assert closed == ["store"]
    assert client.copy_count == 0


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--record-id", "bad:id"],
        ["--record-id", "rec_test", "--request-id", ""],
        ["--record-id", "rec_test", "--request-id", "bad:id"],
    ],
)
def test_command_invalid_arguments_do_not_load_resources(args, monkeypatch):
    monkeypatch.setattr(manual_rerun, "load_config", lambda: pytest.fail("不应读取配置"))
    with pytest.raises(SystemExit) as error:
        manual_rerun.run(args)
    assert error.value.code == 2

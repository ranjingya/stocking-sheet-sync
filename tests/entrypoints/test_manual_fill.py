"""原地填充入口的范围、锁和通知隔离测试。"""

from dataclasses import replace
from pathlib import Path

import pytest

from stocking_sheet_sync.entrypoints import fill
from tests.services.test_sync_service import make_service


@pytest.fixture
def setup(tmp_path, monkeypatch):
    service, client, _, _ = make_service(tmp_path)
    config = replace(service.config, fill_report_dir=str(tmp_path / "reports"))
    monkeypatch.setattr(fill, "load_config", lambda: config)
    monkeypatch.setattr(fill, "build_service", lambda *a: service)
    monkeypatch.setattr(fill, "configure_logging", lambda *a: None)
    calls = []

    class Filler:
        def __init__(self, client, **kwargs):
            self.options = kwargs

        def __call__(self, copy, claim):
            Path(claim.report_path).mkdir(parents=True)
            calls.append((self.options, copy, claim, self.find_sheet(None, None)))
            return {"status": "completed"}

    monkeypatch.setattr(fill, "HistoryFiller", Filler)
    monkeypatch.setattr(fill, "ForecastFiller", Filler)
    return service, client, calls


@pytest.mark.parametrize("flags", [["--history"], ["--forecast"], ["--history", "--forecast"]])
def test_fill_targets_link_without_copy_or_notification(setup, flags):
    service, client, calls = setup
    assert (
        fill.run(
            [
                "--url",
                "https://kocotree.feishu.cn/sheets/token?sheet=tab1",
                "--as-of",
                "2026-09-23",
                *flags,
            ]
        )
        == 0
    )
    options, copy, claim, sheet = calls[0]
    assert copy.source_token == copy.target_token == "token"
    assert sheet == "tab1" and claim.as_of == "2026-09-23"
    assert claim.history_enabled == ("--history" in flags)
    assert claim.forecast_enabled == ("--forecast" in flags)
    assert client.copy_count == 0 and client.sent_cards == []
    assert not Path(claim.report_path).exists()
    token = service.store.acquire_run_lock(300)
    assert token is not None
    service.store.release_run_lock(token)


def test_fill_busy_never_writes(setup):
    service, client, calls = setup
    lock = service.store.acquire_run_lock(300)
    try:
        assert fill.run(["--url", "https://kocotree.feishu.cn/sheets/token", "--history"]) == 3
        assert not calls and client.copy_count == 0
    finally:
        service.store.release_run_lock(lock)


def test_fill_requires_explicit_operation():
    with pytest.raises(SystemExit) as error:
        fill.run(["--url", "https://kocotree.feishu.cn/sheets/token"])
    assert error.value.code == 2


def test_fill_failure_keeps_report_without_delivery_or_notification(setup, monkeypatch):
    service, client, calls = setup

    class Failed:
        def __init__(self, *a, **k):
            pass

        def __call__(self, copy, claim):
            Path(claim.report_path).mkdir(parents=True)
            calls.append(claim)
            return {"status": "needs_review", "reason": "数据不足"}

    monkeypatch.setattr(fill, "HistoryFiller", Failed)
    assert fill.run(["--url", "https://kocotree.feishu.cn/sheets/token", "--history"]) == 2
    assert Path(calls[0].report_path).exists()
    assert client.copy_count == 0 and client.sent_cards == []

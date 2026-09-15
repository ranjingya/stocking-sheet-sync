from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stocking_sheet_sync.config import AppConfig
from stocking_sheet_sync.lark_client import CopyRejected, FeishuApiError, _parse_base_record
from stocking_sheet_sync.models import BaseRecord, CopyResult
from stocking_sheet_sync.redis_store import RedisStateStore
from stocking_sheet_sync.sync_service import SyncBusyError, SyncService, parse_source_sheet
from tests.fakes import FakeRedis


class FakeClient:
    def __init__(self) -> None:
        self.revision = 1
        self.copy_count = 0
        self.copy_names: list[str] = []
        self.sent_to: list[str] = []
        self.status = "需求收集"
        self.source_token = "source-token"
        self.copy_error = ""
        self.record_deleted = False
        self.link_available = True
        self.sent_cards: list[dict[str, Any]] = []

    def get_base_record(self, record_id: str) -> BaseRecord:
        if self.record_deleted:
            raise FeishuApiError("飞书接口失败：RecordIdNotFound", 1, 404)
        link_value = (
            {
                "link": f"https://example.feishu.cn/sheets/{self.source_token}",
                "text": "备货测试表",
                "mentionType": "Sheet",
                "token": self.source_token,
            }
            if self.link_available
            else None
        )
        record = BaseRecord(
            record_id="rec_test",
            shared_url="https://example.feishu.cn/record/rec_test",
            fields={
                "状态": self.status,
                "下单表格": link_value,
            },
        )
        if record.record_id != record_id:
            raise RuntimeError(f"记录不存在：{record_id}")
        return record

    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]:
        return "source-token", "sheet", "备货测试表"

    def copy_spreadsheet(self, spreadsheet_token: str, copy_name: str) -> CopyResult:
        if self.copy_error:
            raise CopyRejected(self.copy_error)
        self.copy_count += 1
        self.copy_names.append(copy_name)
        return CopyResult(
            name=copy_name,
            token=f"target-{self.copy_count}",
            file_type="sheet",
            url=f"https://example.feishu.cn/sheets/target-{self.copy_count}",
        )

    def send_card(self, open_id: str, card: dict[str, Any]) -> None:
        self.sent_to.append(open_id)
        self.sent_cards.append(card)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, minutes: int) -> None:
        self.value += timedelta(minutes=minutes)


def make_config(tmp_path: Path) -> AppConfig:
    del tmp_path
    return AppConfig(
        feishu_data_app_id="cli_data",
        feishu_data_app_secret="data-secret",
        feishu_message_app_id="cli_message",
        feishu_message_app_secret="message-secret",
        feishu_api_base_url="https://open.feishu.cn",
        base_app_token="base-token",
        base_table_id="table-id",
        link_field_name="下单表格",
        required_fields={"状态": "需求收集"},
        target_folder_token="folder-token",
        copy_name_prefix="市场部-",
        notify_open_ids=("ou_test",),
        failure_notify_open_ids=("ou_failure",),
        lock_ttl_seconds=300,
        redis_url="redis://localhost:6379/0",
        redis_key_prefix="stocking-sheet-sync-test",
        request_timeout_seconds=15,
        max_retries=3,
        log_level="INFO",
        public_base_url="https://stock-sync.example.com",
        webhook_secret="webhook-secret",
    )


def test_parse_direct_sheet_and_wiki() -> None:
    sheet = parse_source_sheet(
        {
            "link": "https://example.feishu.cn/sheets/sheet-token",
            "text": "纯表格",
            "mentionType": "Sheet",
            "token": "sheet-token",
        }
    )
    wiki = parse_source_sheet(
        {
            "link": "https://example.feishu.cn/wiki/wiki-token",
            "text": "知识库表格",
            "mentionType": "Wiki",
            "token": "wiki-token",
        }
    )

    assert sheet is not None and sheet.mention_type == "Sheet"
    assert sheet.token == "sheet-token"
    assert wiki is not None and wiki.mention_type == "Wiki"
    assert wiki.token == "wiki-token"


def test_parse_base_record_supports_record_url() -> None:
    record = _parse_base_record(
        {
            "record_id": "rec_test",
            "fields": {"状态": "需求收集"},
            "record_url": "https://example.feishu.cn/record/record-token",
        }
    )

    assert record is not None
    assert record.shared_url == "https://example.feishu.cn/record/record-token"


def make_service(tmp_path):
    client = FakeClient()
    redis = FakeRedis()
    clock = MutableClock()
    store = RedisStateStore("redis://unused", "test", client=redis)
    service = SyncService(make_config(tmp_path), client, client, store, now_provider=clock)
    return service, client, redis, clock


def test_duplicate_after_content_change_and_three_days(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    assert service.run_record("rec_test").result == "copied"
    client.revision += 1
    clock.advance(minutes=60 * 24 * 10)
    assert service.run_record("rec_test").result == "unchanged"
    assert client.copy_count == 1
    assert client.copy_names == ["市场部-备货测试表"]
    state = service.store.get_state("rec_test", client.source_token)
    assert state.target_token == "target-1"
    assert state.copied_at
    assert redis.expirations == {}
    assert client.sent_to == ["ou_test"]


def test_changed_source_copies_once_per_token(tmp_path):
    service, client, _, _ = make_service(tmp_path)
    assert service.run_record("rec_test").result == "copied"
    client.source_token = "other-source"
    assert service.run_record("rec_test").result == "copied"
    client.source_token = "source-token"
    assert service.run_record("rec_test").result == "unchanged"
    assert client.copy_count == 2


def test_wiki_and_direct_link_share_dedup_key(tmp_path, monkeypatch):
    service, client, _, _ = make_service(tmp_path)
    assert service.run_record("rec_test").result == "copied"
    record = client.get_base_record("rec_test")
    record.fields["下单表格"] = {"link": "https://example.feishu.cn/wiki/wiki-token"}
    monkeypatch.setattr(client, "get_base_record", lambda _: record)
    assert service.run_record("rec_test").result == "unchanged"
    assert client.copy_count == 1


def test_filters_and_missing_link_skip_without_claim(tmp_path):
    service, client, redis, _ = make_service(tmp_path)
    client.status = "完成"
    assert service.run_record("rec_test").result == "skipped"
    service.config = replace(service.config, required_fields={})
    client.link_available = False
    assert service.run_record("rec_test").result == "skipped"
    assert not redis.strings
    assert client.copy_count == 0


def test_known_rejection_can_retry(tmp_path):
    service, client, redis, _ = make_service(tmp_path)
    client.copy_error = "没有复制权限"
    assert service.run_record("rec_test").result == "failed"
    assert not redis.strings
    assert client.sent_to == ["ou_failure"]
    client.copy_error = ""
    assert service.run_record("rec_test").result == "copied"


def test_unknown_response_does_not_repeat_copy(tmp_path, monkeypatch):
    service, client, _, _ = make_service(tmp_path)
    real_copy = client.copy_spreadsheet

    def uncertain_copy(token, name):
        real_copy(token, name)
        raise TimeoutError("响应丢失")

    monkeypatch.setattr(client, "copy_spreadsheet", uncertain_copy)
    assert service.run_record("rec_test").result == "failed"
    assert service.run_record("rec_test").result == "failed"
    assert client.copy_count == 1
    assert service.store.get_state("rec_test", client.source_token).status == "copying"


def test_save_failure_preserves_claim(tmp_path, monkeypatch):
    service, client, _, _ = make_service(tmp_path)

    def fail_save(*args):
        raise ConnectionError("Redis 写入中断")

    monkeypatch.setattr(service.store, "finish_copy", fail_save)
    assert service.run_record("rec_test").result == "failed"
    assert service.run_record("rec_test").result == "failed"
    assert client.copy_count == 1


def test_notification_failure_does_not_repeat_copy(tmp_path, monkeypatch):
    service, client, _, _ = make_service(tmp_path)

    def fail_notify(*args):
        assert service.store.get_state("rec_test", client.source_token).status == "copied"
        raise RuntimeError("消息不可达")

    monkeypatch.setattr(client, "send_card", fail_notify)
    assert service.run_record("rec_test").result == "copied"
    assert service.run_record("rec_test").result == "unchanged"
    assert client.copy_count == 1


def test_busy_lock_prevents_processing(tmp_path):
    service, client, _, _ = make_service(tmp_path)
    service.store.acquire_run_lock(300)
    with pytest.raises(SyncBusyError):
        service.run_record("rec_test")
    assert client.copy_count == 0


def test_expired_lock_during_copy_still_prevents_duplicate(tmp_path, monkeypatch):
    service, client, redis, _ = make_service(tmp_path)
    real_copy = client.copy_spreadsheet
    other = SyncService(service.config, client, client, service.store)

    def concurrent_copy(token, name):
        redis.delete("test:lock:scan")
        assert other.run_record("rec_test").result == "failed"
        return real_copy(token, name)

    monkeypatch.setattr(client, "copy_spreadsheet", concurrent_copy)
    assert service.run_record("rec_test").result == "copied"
    assert client.copy_count == 1


def test_legacy_record_prevents_copy(tmp_path):
    service, client, redis, _ = make_service(tmp_path)
    key = "test:rec_test:source-token"
    redis.set(
        key,
        json.dumps(
            {
                "record_id": "rec_test",
                "source_token": "source-token",
                "target_name": "已有副本-v2",
                "target_url": "https://example.feishu.cn/sheets/existing",
                "synced_at": "2026-08-21T10:00:00+08:00",
                "synced_revision": 1,
                "monitor_expires_at": "2026-08-24T00:00:00+08:00",
            }
        ),
        ex=10,
    )
    service.store.migrate_legacy_records()
    assert service.run_record("rec_test").result == "unchanged"
    assert client.copy_count == 0
    assert key not in redis.expirations


def test_corrupt_state_does_not_trigger_copy(tmp_path):
    service, client, redis, _ = make_service(tmp_path)
    redis.set("test:rec_test:source-token", "{invalid}")
    assert service.run_record("rec_test").result == "failed"
    assert client.copy_count == 0


@pytest.mark.parametrize(
    "history,forecast", [(False, False), (True, False), (False, True), (True, True)]
)
def test_fill_flags_copy_once_and_report_each_stage(tmp_path, history, forecast):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(
        service.config, fill_history_enabled=history, fill_forecast_enabled=forecast
    )
    calls = []

    def fill(state, claim):
        assert service.store.get_state(state.record_id, state.source_token).status == "copied"
        assert service.store.get_fill(state).status == "running"
        calls.append((state.target_token, claim.as_of))
        return {"status": "completed"}

    service.history_filler = fill
    first = service.run_record("rec_test")
    clock.advance(minutes=24 * 60 * 10)
    second = service.run_record("rec_test")
    assert client.copy_count == 1
    assert first.history_status == second.history_status == ("completed" if history else "disabled")
    assert first.forecast_status == ("unsupported" if forecast else "disabled")
    assert first.copied == 1 and second.copied == 0
    assert first.failed == 0
    assert first.fill_degraded == forecast
    assert calls == ([("target-1", "2026-08-21")] if history else [])
    if forecast:
        assert "预测规则尚未实现" in first.reason
        assert "搬运成功，填充未完成" in json.dumps(client.sent_cards, ensure_ascii=False)


def test_can_enable_history_on_existing_copy_and_retry_fixed_window(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    assert service.run_record("rec_test").copied == 1
    clock.advance(minutes=24 * 60 * 10)
    service.config = replace(service.config, fill_history_enabled=True)
    dates = []

    def fill(state, claim):
        dates.append(claim.as_of)
        return {
            "status": "retryable" if len(dates) == 1 else "completed",
            "reason": "数据未同步" if len(dates) == 1 else "",
        }

    service.history_filler = fill
    assert service.run_record("rec_test").history_status == "retryable"
    clock.advance(minutes=24 * 60 * 5)
    assert service.run_record("rec_test").history_status == "completed"
    assert service.run_record("rec_test").history_status == "completed"
    assert dates == ["2026-08-21", "2026-08-21"]
    assert client.copy_count == 1


def test_uncertain_fill_is_not_repeated_even_after_lock_expires(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(service.config, fill_history_enabled=True)
    calls = []

    def fill(state, claim):
        calls.append(claim.attempt_id)
        redis.delete("test:lock:scan")
        concurrent = service.run_record("rec_test")
        assert concurrent.failed == 0 and concurrent.history_status == "running"
        assert concurrent.fill_degraded
        assert concurrent.copied == 0
        raise RuntimeError("连接超时，写入结果不确定")

    service.history_filler = fill
    first = service.run_record("rec_test")
    second = service.run_record("rec_test")
    assert first.history_status == second.history_status == "needs_review"
    assert first.copied == 1 and second.copied == 0
    assert len(calls) == client.copy_count == 1
    assert not redis.expirations


def test_fill_state_save_failure_keeps_running_claim(tmp_path, monkeypatch):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(service.config, fill_history_enabled=True)
    calls = []
    service.history_filler = lambda state, claim: calls.append(claim) or {"status": "completed"}

    def fail(*args):
        raise RuntimeError("Redis 保存失败")

    monkeypatch.setattr(service.store, "finish_fill", fail)
    assert service.run_record("rec_test").fill_degraded
    assert service.run_record("rec_test").history_status == "running"
    assert client.copy_count == len(calls) == 1


def test_fill_storage_failure_card_keeps_confirmed_copy_link(tmp_path, monkeypatch):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(service.config, fill_history_enabled=True)

    def fail(*a):
        raise RuntimeError("无法读取填充状态")

    monkeypatch.setattr(service.store, "get_fill", fail)
    result = service.run_record("rec_test")
    assert result.copied == 1 and result.failed == 0
    assert result.fill_degraded
    assert result.history_status == "needs_review"
    assert result.target_url.endswith("target-1")
    card = json.dumps(client.sent_cards[-1], ensure_ascii=False)
    assert "搬运成功，填充未完成" in card
    assert "sheets/target-1" in card


@pytest.mark.parametrize("outcome", ["retryable", "needs_review", "exception"])
def test_fill_failure_returns_successful_copy_through_webhook(tmp_path, outcome):
    from stocking_sheet_sync.web import create_app

    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(service.config, fill_history_enabled=True)
    calls = []

    def fill(state, claim):
        calls.append(claim)
        if outcome == "exception":
            raise RuntimeError("写入结果未知")
        return {"status": outcome, "reason": "历史数据检查未通过"}

    service.history_filler = fill
    app = create_app(config=service.config, service=service).test_client()
    headers = {"Authorization": "Bearer webhook-secret"}
    response = app.post("/webhooks/base-record", json={"record_id": "rec_test"}, headers=headers)
    assert response.status_code == 200
    result = response.get_json()
    assert result["status"] == "success" and result["result"] == "copied"
    assert result["summary"]["failed"] == 0
    assert result["summary"]["fill_degraded"] is True
    assert "填充未完成" in result["reason"]
    assert client.sent_to == ["ou_test"]
    card = client.sent_cards[0]
    assert card["header"]["template"] == "green"
    assert "填充未完成" in card["header"]["title"]["content"]
    assert "处理说明" in json.dumps(card, ensure_ascii=False)
    again = app.post("/webhooks/base-record", json={"record_id": "rec_test"}, headers=headers)
    assert again.status_code == 200 and again.get_json()["result"] == "unchanged"
    assert client.copy_count == 1
    assert len(calls) == (2 if outcome == "retryable" else 1)


def test_force_batches_fill_independently_and_preserve_normal_task(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(service.config, fill_history_enabled=True)
    calls = []
    service.history_filler = lambda state, claim: (
        calls.append((state.request_id, state.target_token, claim.as_of)) or {"status": "completed"}
    )
    ordinary = service.run_record("rec_test")
    saved = dict(redis.strings)
    clock.advance(minutes=24 * 60)
    first = service.run_record("rec_test", force=True, request_id="batch-1")
    repeat = service.run_record("rec_test", force=True, request_id="batch-1")
    second = service.run_record("rec_test", force=True, request_id="batch-2")
    assert ordinary.result == first.result == second.result == "copied"
    assert repeat.result == "unchanged" and repeat.history_status == "completed"
    assert first.force and first.request_id == "batch-1"
    assert client.copy_count == 3
    assert calls == [
        ("", "target-1", "2026-08-21"),
        ("batch-1", "target-2", "2026-08-22"),
        ("batch-2", "target-3", "2026-08-22"),
    ]
    assert all(redis.strings[k] == v for k, v in saved.items())
    assert service.run_record("rec_test").target_url == ordinary.target_url
    assert not redis.expirations


def test_force_claim_survives_service_restart(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    first = service.run_record("rec_test", force=True, request_id="fixed-id")
    new_store = RedisStateStore("redis://unused", "test", client=redis)
    new_store.migrate_legacy_records()
    restarted = SyncService(service.config, client, client, new_store, now_provider=clock)
    again = restarted.run_record("rec_test", force=True, request_id="fixed-id")
    assert again.result == "unchanged" and again.target_url == first.target_url
    assert client.copy_count == 1


def test_force_reused_request_cannot_bind_another_source(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    assert service.run_record("rec_test", force=True, request_id="fixed-id").copied == 1
    client.source_token = "changed-source"
    result = service.run_record("rec_test", force=True, request_id="fixed-id")
    assert result.failed == 1 and "另一份源表格" in result.reason
    assert client.copy_count == 1
    assert service.run_record("rec_test", force=True, request_id="new-id").copied == 1


def test_force_uncertain_copy_is_not_repeated_when_lock_expires(tmp_path, monkeypatch):
    service, client, redis, clock = make_service(tmp_path)
    calls = []

    def uncertain(*args):
        calls.append(args)
        redis.delete("test:lock:scan")
        nested = service.run_record("rec_test", force=True, request_id="fixed-id")
        assert nested.failed == 1
        raise RuntimeError("复制响应未知")

    monkeypatch.setattr(client, "copy_spreadsheet", uncertain)
    assert service.run_record("rec_test", force=True, request_id="fixed-id").failed == 1
    assert service.run_record("rec_test", force=True, request_id="fixed-id").failed == 1
    assert len(calls) == 1


def test_force_known_rejection_can_retry_same_batch(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    client.copy_error = "明确拒绝"
    assert service.run_record("rec_test", force=True, request_id="fixed-id").failed == 1
    client.copy_error = ""
    assert service.run_record("rec_test", force=True, request_id="fixed-id").copied == 1
    assert service.run_record("rec_test", force=True, request_id="fixed-id").result == "unchanged"
    assert client.copy_count == 1


def test_force_obeys_filters_and_fill_failure_fallback(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(service.config, fill_history_enabled=True)
    client.status = "不满足条件"
    assert service.run_record("rec_test", force=True, request_id="fixed-id").result == "skipped"
    assert client.copy_count == 0
    client.status = "需求收集"
    service.history_filler = lambda *a: {"status": "needs_review", "reason": "填充未完成"}
    result = service.run_record("rec_test", force=True, request_id="fixed-id")
    assert result.result == "copied" and result.fill_degraded and result.failed == 0
    again = service.run_record("rec_test", force=True, request_id="fixed-id")
    assert again.result == "unchanged" and again.fill_degraded
    assert client.copy_count == 1

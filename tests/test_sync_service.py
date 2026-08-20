from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from stocking_sheet_sync.config import AppConfig
from stocking_sheet_sync.lark_client import _parse_base_record
from stocking_sheet_sync.models import BaseRecord, CopyResult
from stocking_sheet_sync.redis_store import RedisStateStore
from stocking_sheet_sync.sync_service import SyncService, parse_source_sheet
from tests.fakes import FakeRedis


class FakeClient:
    def __init__(self) -> None:
        self.revision = 1
        self.copy_count = 0
        self.sent_to: list[str] = []

    def list_base_records(self) -> list[BaseRecord]:
        return [
            BaseRecord(
                record_id="rec_test",
                shared_url="https://example.feishu.cn/record/rec_test",
                fields={
                    "状态": "需求收集",
                    "下单表格": {
                        "link": "https://example.feishu.cn/sheets/source-token",
                        "text": "备货测试表",
                        "mentionType": "Sheet",
                        "token": "source-token",
                    },
                },
            )
        ]

    def get_base_record(self, record_id: str) -> BaseRecord:
        record = self.list_base_records()[0]
        if record.record_id != record_id:
            raise RuntimeError(f"记录不存在：{record_id}")
        return record

    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]:
        return "source-token", "sheet", "备货测试表"

    def get_spreadsheet_revision(self, spreadsheet_token: str) -> tuple[int, str]:
        return self.revision, "备货测试表"

    def copy_spreadsheet(self, spreadsheet_token: str, copy_name: str) -> CopyResult:
        self.copy_count += 1
        return CopyResult(
            name=copy_name,
            token=f"target-{self.copy_count}",
            file_type="sheet",
            url=f"https://example.feishu.cn/sheets/target-{self.copy_count}",
        )

    def send_card(self, open_id: str, card: dict[str, Any]) -> None:
        self.sent_to.append(open_id)


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
        base_view_id=None,
        link_field_name="下单表格",
        required_fields={"状态": "需求收集"},
        target_folder_token="folder-token",
        copy_name_prefix="市场部-",
        notify_open_ids=("ou_test",),
        poll_interval_minutes=10,
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


def test_revision_change_copies_again_and_same_revision_skips(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    data_client = FakeClient()
    message_client = FakeClient()
    store = RedisStateStore(config.redis_url, config.redis_key_prefix, client=FakeRedis())
    service = SyncService(config, data_client, message_client, store)
    try:
        first = service.run_once()
        second = service.run_once()
        data_client.revision = 2
        third = service.run_once()
    finally:
        store.close()

    assert first.copied == 1
    assert second.unchanged == 1
    assert third.copied == 1
    assert data_client.copy_count == 2
    assert data_client.sent_to == []
    assert message_client.sent_to == ["ou_test", "ou_test"]


def test_baseline_only_seeds_new_record(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    data_client = FakeClient()
    message_client = FakeClient()
    store = RedisStateStore(config.redis_url, config.redis_key_prefix, client=FakeRedis())
    service = SyncService(config, data_client, message_client, store)
    try:
        baseline = service.run_once(baseline=True)
        unchanged = service.run_once()
        data_client.revision = 2
        changed = service.run_once()
    finally:
        store.close()

    assert baseline.baselined == 1
    assert unchanged.unchanged == 1
    assert changed.copied == 1
    assert data_client.copy_count == 1


def test_webhook_record_only_processes_requested_record(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    data_client = FakeClient()
    message_client = FakeClient()
    store = RedisStateStore(config.redis_url, config.redis_key_prefix, client=FakeRedis())
    service = SyncService(config, data_client, message_client, store)
    try:
        first = service.run_record("rec_test")
        second = service.run_record("rec_test")
    finally:
        store.close()

    assert first.scanned == 1
    assert first.copied == 1
    assert second.unchanged == 1
    assert data_client.copy_count == 1


def test_scheduled_scan_only_logs_when_content_changes(tmp_path: Path, caplog) -> None:
    config = make_config(tmp_path)
    data_client = FakeClient()
    message_client = FakeClient()
    store = RedisStateStore(config.redis_url, config.redis_key_prefix, client=FakeRedis())
    logger = logging.getLogger("stocking_sheet_sync.scheduled_test")
    service = SyncService(config, data_client, message_client, store, logger)
    caplog.set_level(logging.INFO, logger=logger.name)
    try:
        caplog.clear()
        service.run_once()
        changed_messages = [
            record.getMessage() for record in caplog.records if record.name == logger.name
        ]

        caplog.clear()
        service.run_once()
        unchanged_messages = [
            record.getMessage() for record in caplog.records if record.name == logger.name
        ]
    finally:
        store.close()

    assert changed_messages == [
        "产品下单同步扫描完成：scanned=1 copied=1 unchanged=0 baselined=0 failed=0"
    ]
    assert unchanged_messages == []

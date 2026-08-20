from __future__ import annotations

from pathlib import Path
from typing import Any

from stocking_sheet_sync.config import AppConfig
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
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
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


def test_revision_change_copies_again_and_same_revision_skips(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeClient()
    store = RedisStateStore(
        config.redis_url, config.redis_key_prefix, client=FakeRedis()
    )
    service = SyncService(config, client, store)
    try:
        first = service.run_once()
        second = service.run_once()
        client.revision = 2
        third = service.run_once()
    finally:
        store.close()

    assert first.copied == 1
    assert second.unchanged == 1
    assert third.copied == 1
    assert client.copy_count == 2
    assert client.sent_to == ["ou_test", "ou_test"]


def test_baseline_only_seeds_new_record(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    client = FakeClient()
    store = RedisStateStore(
        config.redis_url, config.redis_key_prefix, client=FakeRedis()
    )
    service = SyncService(config, client, store)
    try:
        baseline = service.run_once(baseline=True)
        unchanged = service.run_once()
        client.revision = 2
        changed = service.run_once()
    finally:
        store.close()

    assert baseline.baselined == 1
    assert unchanged.unchanged == 1
    assert changed.copied == 1
    assert client.copy_count == 1

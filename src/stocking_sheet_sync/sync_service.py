from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

from .card import build_sync_card
from .config import AppConfig
from .models import (
    BaseRecord,
    CopyResult,
    ResolvedSheet,
    SourceSheet,
    SyncedRecord,
    SyncSummary,
)


class DataClient(Protocol):
    def list_base_records(self) -> list[BaseRecord]: ...

    def get_base_record(self, record_id: str) -> BaseRecord: ...

    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]: ...

    def get_spreadsheet_revision(self, spreadsheet_token: str) -> tuple[int, str]: ...

    def copy_spreadsheet(self, spreadsheet_token: str, copy_name: str) -> CopyResult: ...


class MessageClient(Protocol):
    def send_card(self, open_id: str, card: dict[str, Any]) -> None: ...


class SyncedStore(Protocol):
    def acquire_run_lock(self, ttl_minutes: float) -> bool: ...

    def release_run_lock(self) -> None: ...

    def is_synced(self, record_id: str, source_token: str, source_revision: int) -> bool: ...

    def save_synced(self, record: SyncedRecord) -> None: ...


class SyncBusyError(RuntimeError):
    """同步任务已经在其他进程中运行。"""


class SyncService:
    def __init__(
        self,
        config: AppConfig,
        data_client: DataClient,
        message_client: MessageClient,
        store: SyncedStore,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.data_client = data_client
        self.message_client = message_client
        self.store = store
        self.logger = logger or logging.getLogger(__name__)

    def run_once(self, *, baseline: bool = False) -> SyncSummary:
        """
        功能说明：扫描多维表记录，检测源表 revision 并按需复制和通知。

        参数：
            baseline：是否只将当前表格版本记为已同步，不执行复制和通知。

        返回值：
            本轮扫描、复制、跳过、基线和失败数量。
        """
        summary = SyncSummary()
        lock_ttl = max(self.config.poll_interval_minutes * 2, 30)
        if not self.store.acquire_run_lock(lock_ttl):
            self.logger.warning("已有同步任务正在运行，本轮跳过")
            return summary

        self.logger.info("开始扫描产品下单记录：baseline=%s", baseline)
        try:
            for record in self.data_client.list_base_records():
                if not matches_required_fields(record.fields, self.config.required_fields):
                    continue
                summary.scanned += 1
                self._process_record(record, baseline, summary)
            self.logger.info(
                "产品下单同步扫描完成：scanned=%d copied=%d unchanged=%d baselined=%d failed=%d",
                summary.scanned,
                summary.copied,
                summary.unchanged,
                summary.baselined,
                summary.failed,
            )
            return summary
        finally:
            self.store.release_run_lock()

    def run_record(self, record_id: str) -> SyncSummary:
        """
        功能说明：读取并同步 Webhook 指定的一条多维表记录。

        参数：
            record_id：多维表自动化传入的记录 ID。

        返回值：本次单记录处理的扫描、复制、跳过和失败数量。
        """
        summary = SyncSummary()
        lock_ttl = max(self.config.poll_interval_minutes * 2, 30)
        if not self.store.acquire_run_lock(lock_ttl):
            raise SyncBusyError("已有同步任务正在运行")

        self.logger.info("开始处理 Webhook 触发记录：record_id=%s", record_id)
        try:
            record = self.data_client.get_base_record(record_id)
            if not matches_required_fields(record.fields, self.config.required_fields):
                summary.unchanged += 1
                self.logger.info(
                    "Webhook 触发记录不符合筛选条件：record_id=%s",
                    record.record_id,
                )
                return summary

            summary.scanned += 1
            self._process_record(record, False, summary)
            self.logger.info(
                "Webhook 触发记录处理完成：record_id=%s copied=%d unchanged=%d failed=%d",
                record.record_id,
                summary.copied,
                summary.unchanged,
                summary.failed,
            )
            return summary
        finally:
            self.store.release_run_lock()

    def _process_record(self, record: BaseRecord, baseline: bool, summary: SyncSummary) -> None:
        source: SourceSheet | None = None
        resolved: ResolvedSheet | None = None
        record_url = record.shared_url.strip()

        try:
            source = parse_source_sheet(record.fields.get(self.config.link_field_name))
            if source is None:
                summary.unchanged += 1
                self.logger.info("记录没有可同步的表格链接：record_id=%s", record.record_id)
                return
            if not record_url:
                raise RuntimeError("多维表接口未返回原始记录链接 shared_url")

            resolved = self._resolve_source(source)
            if self.store.is_synced(
                record.record_id,
                resolved.token,
                resolved.revision,
            ):
                summary.unchanged += 1
                self.logger.info(
                    "源表格版本已同步：record_id=%s revision=%d",
                    record.record_id,
                    resolved.revision,
                )
                return

            synced_at = format_shanghai_time(datetime.now(UTC))
            if baseline:
                self.store.save_synced(
                    SyncedRecord(
                        record_id=record.record_id,
                        source_token=resolved.token,
                        source_revision=resolved.revision,
                        source_name=resolved.title,
                        source_url=resolved.source_url,
                        record_url=record_url,
                        target_name="",
                        target_url="",
                        synced_at=synced_at,
                    )
                )
                summary.baselined += 1
                self.logger.info(
                    "已将源表格版本记为同步基线：record_id=%s revision=%d",
                    record.record_id,
                    resolved.revision,
                )
                return

            copy_name = f"{self.config.copy_name_prefix}{resolved.title}"
            self.logger.info(
                "检测到未同步版本，开始复制：record_id=%s revision=%d copy_name=%s",
                record.record_id,
                resolved.revision,
                copy_name,
            )
            copied = self.data_client.copy_spreadsheet(resolved.token, copy_name)
            target_name = copied.name or copy_name

            self.store.save_synced(
                SyncedRecord(
                    record_id=record.record_id,
                    source_token=resolved.token,
                    source_revision=resolved.revision,
                    source_name=resolved.title,
                    source_url=resolved.source_url,
                    record_url=record_url,
                    target_name=target_name,
                    target_url=copied.url,
                    synced_at=synced_at,
                )
            )

            card = build_sync_card(
                original_name=resolved.title,
                record_url=record_url,
                target_name=target_name,
                target_url=copied.url,
                status="success",
            )
            failed_count = self._notify(list(self.config.notify_open_ids), card)
            summary.copied += 1
            self.logger.info(
                "电子表格复制完成：record_id=%s revision=%d target_url=%s "
                "failed_notification_count=%d",
                record.record_id,
                resolved.revision,
                copied.url,
                failed_count,
            )
        except Exception as error:
            summary.failed += 1
            message = str(error)
            self.logger.exception("处理记录失败：record_id=%s", record.record_id)
            if record_url and self.config.notify_open_ids:
                card = build_sync_card(
                    original_name=(
                        resolved.title if resolved else source.title if source else "未知表格"
                    ),
                    record_url=record_url,
                    status="failure",
                    reason=message,
                )
                self._notify(list(self.config.notify_open_ids), card)

    def _resolve_source(self, source: SourceSheet) -> ResolvedSheet:
        token = source.token
        title = source.title
        if source.mention_type == "Wiki":
            token, document_type, wiki_title = self.data_client.resolve_wiki_node(source.token)
            if document_type != "sheet":
                raise RuntimeError(f"链接对应的文档不是电子表格，而是 {document_type}")
            title = wiki_title or title

        revision, metadata_title = self.data_client.get_spreadsheet_revision(token)
        return ResolvedSheet(
            token=token,
            title=title or metadata_title or "未命名表格",
            revision=revision,
            source_url=source.source_url,
        )

    def _notify(self, open_ids: list[str], card: dict[str, Any]) -> int:
        failed_count = 0
        for open_id in open_ids:
            try:
                self.message_client.send_card(open_id, card)
                self.logger.info("同步通知发送成功：open_id=%s", open_id)
            except Exception:
                failed_count += 1
                self.logger.exception("同步通知发送失败：open_id=%s", open_id)
        return failed_count


def parse_source_sheet(value: Any) -> SourceSheet | None:
    """
    功能说明：从多维表超链接字段中解析 Wiki 或直接 Sheet 链接。

    参数：
        value：多维表字段的原始值。

    返回值：
        标准化的源表格信息；字段为空或格式不受支持时返回 None。
    """
    candidate = _unwrap_link_value(value)
    if candidate is None:
        return None
    link = _string_value(candidate.get("link"))
    token = _string_value(candidate.get("token")) or _extract_token(link)
    title = _string_value(candidate.get("text")) or "未命名表格"
    raw_type = _string_value(candidate.get("mentionType"))
    if raw_type == "Wiki" or "/wiki/" in link.lower():
        mention_type = "Wiki"
    elif raw_type == "Sheet" or "/sheets/" in link.lower():
        mention_type = "Sheet"
    else:
        return None
    if not token or not link:
        return None
    return SourceSheet(
        token=token,
        title=title,
        source_url=link,
        mention_type=mention_type,
    )


def matches_required_fields(fields: dict[str, Any], required: dict[str, Any]) -> bool:
    return all(
        _normalize_comparable(fields.get(name)) == _normalize_comparable(expected)
        for name, expected in required.items()
    )


def format_shanghai_time(value: datetime) -> str:
    shanghai_timezone = timezone(timedelta(hours=8))
    return value.astimezone(shanghai_timezone).isoformat(timespec="seconds")


def _unwrap_link_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list):
        return _unwrap_link_value(value[0]) if value else None
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("link"), str) or isinstance(value.get("token"), str):
        return value
    nested = value.get("value")
    return _unwrap_link_value(nested) if isinstance(nested, list) else None


def _extract_token(link: str) -> str:
    matched = re.search(r"/(?:wiki|sheets)/([^/?#]+)", link, flags=re.IGNORECASE)
    return matched.group(1) if matched else ""


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_comparable(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalize_comparable(item) for item in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("text"), str):
        return value["text"]
    if "value" in value:
        return _normalize_comparable(value["value"])
    return value

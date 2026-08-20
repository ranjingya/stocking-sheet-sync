from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

from .card import build_sync_card
from .config import AppConfig
from .models import BaseRecord, CopyResult, ResolvedSheet, SourceSheet, SyncState, SyncSummary


class SyncClient(Protocol):
    def list_base_records(self) -> list[BaseRecord]: ...

    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]: ...

    def get_spreadsheet_revision(self, spreadsheet_token: str) -> tuple[int, str]: ...

    def copy_spreadsheet(self, spreadsheet_token: str, copy_name: str) -> CopyResult: ...

    def send_card(self, open_id: str, card: dict[str, Any]) -> None: ...


class SyncStateStore(Protocol):
    def acquire_run_lock(self, ttl_minutes: float) -> bool: ...

    def release_run_lock(self) -> None: ...

    def get(self, record_id: str) -> SyncState | None: ...

    def save_baseline(
        self,
        *,
        record_id: str,
        source_token: str,
        source_revision: int,
        original_name: str,
        record_url: str,
    ) -> None: ...

    def save(self, state: SyncState) -> None: ...

    def update_pending_notifications(self, record_id: str, open_ids: list[str]) -> None: ...


class SyncService:
    def __init__(
        self,
        config: AppConfig,
        client: SyncClient,
        store: SyncStateStore,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.logger = logger or logging.getLogger(__name__)

    def run_once(self, *, baseline: bool = False) -> SyncSummary:
        """
        功能说明：扫描多维表记录，检测源表 revision 并按需复制和通知。

        参数：
            baseline：是否仅为尚未接管的记录建立当前 revision 基线。

        返回值：
            本轮扫描、复制、跳过、失败和通知重试数量。
        """
        summary = SyncSummary()
        lock_ttl = max(self.config.poll_interval_minutes * 2, 30)
        if not self.store.acquire_run_lock(lock_ttl):
            self.logger.warning("已有同步任务正在运行，本轮跳过")
            return summary

        self.logger.info("开始扫描产品下单记录：baseline=%s", baseline)
        try:
            for record in self.client.list_base_records():
                if not matches_required_fields(record.fields, self.config.required_fields):
                    continue
                summary.scanned += 1
                self._process_record(record, baseline, summary)
            self.logger.info(
                "产品下单同步扫描完成：scanned=%d copied=%d unchanged=%d "
                "baselined=%d failed=%d notifications_retried=%d",
                summary.scanned,
                summary.copied,
                summary.unchanged,
                summary.baselined,
                summary.failed,
                summary.notifications_retried,
            )
            return summary
        finally:
            self.store.release_run_lock()

    def _process_record(
        self, record: BaseRecord, baseline: bool, summary: SyncSummary
    ) -> None:
        source: SourceSheet | None = None
        resolved: ResolvedSheet | None = None
        previous = self.store.get(record.record_id)
        record_url = record.shared_url.strip() or (previous.record_url if previous else "")

        try:
            source = parse_source_sheet(record.fields.get(self.config.link_field_name))
            if source is None:
                summary.unchanged += 1
                self.logger.info(
                    "记录没有可同步的表格链接：record_id=%s", record.record_id
                )
                return
            if not record_url:
                raise RuntimeError("多维表接口未返回原始记录链接 shared_url")

            resolved = self._resolve_source(source)
            if baseline:
                if previous is None:
                    self.store.save_baseline(
                        record_id=record.record_id,
                        source_token=resolved.token,
                        source_revision=resolved.revision,
                        original_name=resolved.title,
                        record_url=record_url,
                    )
                    summary.baselined += 1
                    self.logger.info(
                        "已建立源表格 revision 基线：record_id=%s revision=%d",
                        record.record_id,
                        resolved.revision,
                    )
                else:
                    summary.unchanged += 1
                    self.logger.info(
                        "记录已有状态，基线模式不覆盖：record_id=%s", record.record_id
                    )
                return

            if _is_same_synced_revision(previous, resolved):
                if previous and previous.pending_notify_open_ids:
                    self._retry_pending_notifications(previous)
                    summary.notifications_retried += 1
                else:
                    summary.unchanged += 1
                    self.logger.info(
                        "源表格 revision 未变化：record_id=%s revision=%d",
                        record.record_id,
                        resolved.revision,
                    )
                return

            copy_name = f"{self.config.copy_name_prefix}{resolved.title}"
            self.logger.info(
                "检测到未同步版本，开始复制：record_id=%s previous_revision=%s "
                "current_revision=%d copy_name=%s",
                record.record_id,
                previous.source_revision if previous else None,
                resolved.revision,
                copy_name,
            )
            copied = self.client.copy_spreadsheet(resolved.token, copy_name)
            now = datetime.now(UTC)
            state = SyncState(
                record_id=record.record_id,
                source_token=resolved.token,
                source_revision=resolved.revision,
                original_name=resolved.title,
                record_url=record_url,
                target_token=copied.token,
                target_name=copied.name or copy_name,
                target_url=copied.url,
                copied_at=format_shanghai_time(now),
                status="success",
                pending_notify_open_ids=list(self.config.notify_open_ids),
                updated_at=now.isoformat(),
            )

            # 先保存复制结果，消息失败时只重试消息，避免重复复制同一 revision。
            self.store.save(state)
            card = build_sync_card(
                original_name=resolved.title,
                record_url=record_url,
                target_name=copied.name or copy_name,
                target_url=copied.url,
                status="success",
            )
            failed_open_ids = self._notify(list(self.config.notify_open_ids), card)
            self.store.update_pending_notifications(record.record_id, failed_open_ids)
            summary.copied += 1
            self.logger.info(
                "电子表格复制完成：record_id=%s revision=%d target_url=%s "
                "failed_notification_count=%d",
                record.record_id,
                resolved.revision,
                copied.url,
                len(failed_open_ids),
            )
        except Exception as error:
            summary.failed += 1
            message = str(error)
            self.logger.exception("处理记录失败：record_id=%s", record.record_id)
            self._handle_failure(
                record=record,
                source=source,
                resolved=resolved,
                previous=previous,
                record_url=record_url,
                message=message,
            )

    def _resolve_source(self, source: SourceSheet) -> ResolvedSheet:
        token = source.token
        title = source.title
        if source.mention_type == "Wiki":
            token, document_type, wiki_title = self.client.resolve_wiki_node(source.token)
            if document_type != "sheet":
                raise RuntimeError(
                    f"链接对应的文档不是电子表格，而是 {document_type}"
                )
            title = wiki_title or title

        revision, metadata_title = self.client.get_spreadsheet_revision(token)
        return ResolvedSheet(
            token=token,
            title=title or metadata_title or "未命名表格",
            revision=revision,
            source_url=source.source_url,
        )

    def _retry_pending_notifications(self, state: SyncState) -> None:
        if not state.target_name or not state.target_url:
            return
        card = build_sync_card(
            original_name=state.original_name,
            record_url=state.record_url,
            target_name=state.target_name,
            target_url=state.target_url,
            status="success",
        )
        failed = self._notify(state.pending_notify_open_ids, card)
        self.store.update_pending_notifications(state.record_id, failed)

    def _handle_failure(
        self,
        *,
        record: BaseRecord,
        source: SourceSheet | None,
        resolved: ResolvedSheet | None,
        previous: SyncState | None,
        record_url: str,
        message: str,
    ) -> None:
        now = datetime.now(UTC)
        should_notify = (
            bool(record_url)
            and bool(self.config.notify_open_ids)
            and _should_notify_error(
                previous,
                message,
                now,
                self.config.error_notify_cooldown_minutes,
            )
        )
        notified_at = previous.last_error_notified_at if previous else None
        if should_notify:
            card = build_sync_card(
                original_name=(
                    resolved.title
                    if resolved
                    else source.title
                    if source
                    else previous.original_name
                    if previous
                    else "未知表格"
                ),
                record_url=record_url,
                status="failure",
                reason=message,
            )
            self._notify(list(self.config.notify_open_ids), card)
            notified_at = now.isoformat()

        # 保留上一次成功 revision；临时故障恢复后不会误复制同一版本。
        self.store.save(
            SyncState(
                record_id=record.record_id,
                source_token=(
                    previous.source_token
                    if previous
                    else resolved.token
                    if resolved
                    else source.token
                    if source
                    else "unknown"
                ),
                source_revision=(
                    previous.source_revision
                    if previous
                    else resolved.revision
                    if resolved
                    else -1
                ),
                original_name=(
                    resolved.title
                    if resolved
                    else source.title
                    if source
                    else previous.original_name
                    if previous
                    else "未知表格"
                ),
                record_url=record_url or (previous.record_url if previous else ""),
                target_token=previous.target_token if previous else None,
                target_name=previous.target_name if previous else None,
                target_url=previous.target_url if previous else None,
                copied_at=previous.copied_at if previous else None,
                status=previous.status if previous else "error",
                pending_notify_open_ids=(
                    list(previous.pending_notify_open_ids) if previous else []
                ),
                last_error=message,
                last_error_notified_at=notified_at,
                updated_at=now.isoformat(),
            )
        )

    def _notify(self, open_ids: list[str], card: dict[str, Any]) -> list[str]:
        failed: list[str] = []
        for open_id in open_ids:
            try:
                self.client.send_card(open_id, card)
                self.logger.info("同步通知发送成功：open_id=%s", open_id)
            except Exception:
                failed.append(open_id)
                self.logger.exception("同步通知发送失败：open_id=%s", open_id)
        return failed


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


def _is_same_synced_revision(state: SyncState | None, sheet: ResolvedSheet) -> bool:
    return bool(
        state
        and state.status in {"success", "baseline"}
        and state.source_token == sheet.token
        and state.source_revision == sheet.revision
    )


def _should_notify_error(
    previous: SyncState | None,
    message: str,
    now: datetime,
    cooldown_minutes: float,
) -> bool:
    if not previous or not previous.last_error_notified_at or previous.last_error != message:
        return True
    try:
        previous_time = datetime.fromisoformat(previous.last_error_notified_at)
    except ValueError:
        return True
    if previous_time.tzinfo is None:
        previous_time = previous_time.replace(tzinfo=UTC)
    return now - previous_time >= timedelta(minutes=cooldown_minutes)

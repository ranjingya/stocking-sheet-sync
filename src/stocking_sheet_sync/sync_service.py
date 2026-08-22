from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Protocol

from .card import build_sync_card
from .config import AppConfig
from .lark_client import FeishuApiError
from .models import (
    BaseRecord,
    CopyResult,
    ResolvedSheet,
    SourceSheet,
    SyncedRecord,
    SyncedSheetState,
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
    def acquire_run_lock(self, ttl_minutes: float) -> str | None: ...

    def release_run_lock(self, token: str) -> None: ...

    def is_synced(self, record_id: str, source_token: str, source_revision: int) -> bool: ...

    def get_state(self, record_id: str, source_token: str) -> SyncedSheetState | None: ...

    def save_synced(self, record: SyncedRecord) -> None: ...

    def next_copy_version(self, record_id: str, source_token: str) -> int: ...

    def list_latest_synced(self) -> list[SyncedSheetState]: ...

    def save_pending(
        self,
        state: SyncedSheetState,
        pending_revision: int | None,
        pending_since: str = "",
    ) -> None: ...

    def delete_state(self, record_id: str, source_token: str) -> None: ...


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
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.data_client = data_client
        self.message_client = message_client
        self.store = store
        self.logger = logger or logging.getLogger(__name__)
        self._now_provider = now_provider or (lambda: datetime.now(UTC))

    def run_once(self, *, check_all: bool = True) -> SyncSummary:
        """
        功能说明：从 Redis 读取已接管表格，检查 revision 并按静默窗口复制和通知。

        参数：
            check_all：是否检查全部已接管表格；为 False 时只检查观察中的表格。

        返回值：
            本轮扫描、复制、跳过和失败数量。
        """
        summary = SyncSummary()
        lock_ttl = max(self.config.poll_interval_minutes * 2, 30)
        lock_token = self.store.acquire_run_lock(lock_ttl)
        if lock_token is None:
            summary.result = "busy"
            summary.reason = "已有同步任务正在运行"
            self.logger.warning(
                "Worker 本轮检查结束：result=busy reason=%s",
                summary.reason,
            )
            return summary

        try:
            all_states = self.store.list_latest_synced()
            states = (
                all_states
                if check_all
                else [state for state in all_states if state.pending_revision is not None]
            )
            if check_all:
                self.logger.info("开始常规检查：监听表格=%d", len(states))
            elif states:
                self.logger.info("开始变动检查：观察中=%d", len(states))

            for state in states:
                summary.scanned += 1
                self._check_synced_sheet(
                    state,
                    summary,
                    verify_monitor=check_all,
                )
            _finalize_summary_result(summary)
            if check_all or states:
                self.logger.info(
                    "本轮检查完成：检查=%d 无变化=%d 观察中=%d 搬运=%d "
                    "跳过=%d 失败=%d",
                    summary.scanned,
                    summary.unchanged,
                    summary.observing,
                    summary.copied,
                    summary.skipped,
                    summary.failed,
                )
            return summary
        finally:
            self.store.release_run_lock(lock_token)

    def run_record(self, record_id: str) -> SyncSummary:
        """
        功能说明：读取并同步 Webhook 指定的一条多维表记录。

        参数：
            record_id：多维表自动化传入的记录 ID。

        返回值：本次单记录处理的扫描、复制、跳过和失败数量。
        """
        summary = SyncSummary()
        lock_ttl = max(self.config.poll_interval_minutes * 2, 30)
        lock_token = self.store.acquire_run_lock(lock_ttl)
        if lock_token is None:
            raise SyncBusyError("已有同步任务正在运行")

        self.logger.debug("开始处理 Webhook 触发记录：record_id=%s", record_id)
        try:
            record = self.data_client.get_base_record(record_id)
            if not matches_required_fields(
                record.fields,
                self.config.required_fields,
            ):
                summary.skipped += 1
                summary.result = "skipped"
                summary.reason = "不符合首次搬运条件"
                self.logger.debug(
                    "Webhook 触发记录不符合筛选条件：record_id=%s reason=%s",
                    record.record_id,
                    summary.reason,
                )
                return summary

            summary.scanned += 1
            self._process_record(record, summary)
            self.logger.debug(
                "Webhook 触发记录处理完成：record_id=%s copied=%d unchanged=%d failed=%d",
                record.record_id,
                summary.copied,
                summary.unchanged,
                summary.failed,
            )
            return summary
        finally:
            self.store.release_run_lock(lock_token)

    def _process_record(self, record: BaseRecord, summary: SyncSummary) -> None:
        source: SourceSheet | None = None
        resolved: ResolvedSheet | None = None
        record_url = record.shared_url.strip()

        try:
            source = parse_source_sheet(record.fields.get(self.config.link_field_name))
            if source is None:
                summary.skipped += 1
                summary.result = "skipped"
                summary.reason = "表格链接为空或格式不支持"
                self.logger.debug(
                    "记录没有可同步的表格链接：record_id=%s reason=%s",
                    record.record_id,
                    summary.reason,
                )
                return
            if not record_url:
                raise RuntimeError("多维表接口未返回原始记录链接 shared_url")

            resolved = self._resolve_source(source)
            current_state = self.store.get_state(record.record_id, resolved.token)
            if self.store.is_synced(
                record.record_id,
                resolved.token,
                resolved.revision,
            ):
                summary.unchanged += 1
                summary.result = "unchanged"
                summary.reason = "当前版本已同步"
                self.logger.debug(
                    "源表格版本已同步：record_id=%s revision=%d",
                    record.record_id,
                    resolved.revision,
                )
                return

            if current_state is not None:
                if current_state.pending_revision != resolved.revision:
                    self.store.save_pending(
                        current_state,
                        resolved.revision,
                        format_shanghai_time(self._now()),
                    )
                    self.logger.info(
                        "Webhook 检测到已监听表格变化，开始静默观察："
                        "record_id=%s revision=%d",
                        record.record_id,
                        resolved.revision,
                    )
                summary.observing += 1
                summary.result = "observing"
                summary.reason = "等待静默期结束"
                return

            synced_at = format_shanghai_time(self._now())
            copy_version = self.store.next_copy_version(record.record_id, resolved.token)
            copy_name = build_copy_name(
                self.config.copy_name_prefix,
                resolved.title,
                copy_version,
            )
            self.logger.debug(
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
                    copy_version=copy_version,
                )
            )

            card = build_sync_card(
                original_name=resolved.title,
                record_url=record_url,
                target_name=target_name,
                target_url=copied.url,
                status="success",
                sync_type="initial",
                target_folder_token=self.config.target_folder_token,
            )
            failed_count = self._notify(list(self.config.notify_open_ids), card)
            summary.copied += 1
            summary.result = "copied"
            summary.reason = ""
            self.logger.debug(
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
            summary.result = "failed"
            summary.reason = message
            self.logger.error(
                "记录同步失败：record_id=%s reason=%s",
                record.record_id,
                message,
            )
            self.logger.debug(
                "记录同步失败堆栈：record_id=%s",
                record.record_id,
                exc_info=True,
            )
            if record_url and self.config.failure_notify_open_ids:
                card = build_sync_card(
                    original_name=(
                        resolved.title if resolved else source.title if source else "未知表格"
                    ),
                    record_url=record_url,
                    status="failure",
                    sync_type="initial",
                    target_folder_token=self.config.target_folder_token,
                    reason=message,
                )
                self._notify(list(self.config.failure_notify_open_ids), card)

    def _handle_deleted_record(
        self,
        state: SyncedSheetState,
        summary: SyncSummary,
    ) -> None:
        """
        功能说明：通知原始记录已删除，并清理对应的 Redis 监听状态。

        参数：
            state：原记录删除前保存的最新同步状态。
            summary：用于累计本轮跳过结果的汇总对象。

        返回值：无。
        """
        card = build_sync_card(
            original_name=state.source_name or "未知表格",
            record_url=state.record_url,
            target_name=state.target_name,
            target_url=state.target_url,
            status="deleted",
            sync_type="update",
            target_folder_token=self.config.target_folder_token,
        )
        failed_count = self._notify(list(self.config.failure_notify_open_ids), card)
        self.store.delete_state(state.record_id, state.source_token)
        summary.skipped += 1
        self.logger.warning(
            "Worker 表格检查完成：record_id=%s name=%s result=skipped "
            "reason=原始记录已删除 redis_key_deleted=true failed_notification_count=%d",
            state.record_id,
            state.source_name,
            failed_count,
        )

    def _check_synced_sheet(
        self,
        state: SyncedSheetState,
        summary: SyncSummary,
        *,
        verify_monitor: bool,
    ) -> None:
        """
        功能说明：检查一张已接管表格，并维护 revision 静默观察状态。

        参数：
            state：Redis 中该表格的最新同步状态。
            summary：用于累计本轮处理结果的汇总对象。
            verify_monitor：本轮是否需要重新验证记录状态和当前表格链接。

        返回值：无。
        """
        now = self._now()
        try:
            ineligibility_reason = (
                self._monitor_ineligibility_reason(state) if verify_monitor else None
            )
            if ineligibility_reason is not None:
                if state.pending_revision is not None:
                    self.store.save_pending(state, None)
                summary.skipped += 1
                self.logger.info(
                    "Worker 表格检查完成：record_id=%s name=%s result=skipped reason=%s",
                    state.record_id,
                    state.source_name,
                    ineligibility_reason,
                )
                return

            online_revision, metadata_title = self.data_client.get_spreadsheet_revision(
                state.source_token
            )
            if online_revision == state.synced_revision:
                if state.pending_revision is not None:
                    self.store.save_pending(state, None)
                    self.logger.info(
                        "Worker 表格检查完成：record_id=%s name=%s revision=%d "
                        "result=unchanged reason=表格已恢复到同步版本",
                        state.record_id,
                        state.source_name,
                        online_revision,
                    )
                summary.unchanged += 1
                return

            if state.pending_revision is None:
                self.store.save_pending(
                    state,
                    online_revision,
                    format_shanghai_time(now),
                )
                summary.observing += 1
                self.logger.info(
                    "Worker 表格检查完成：record_id=%s name=%s revision=%d "
                    "result=observing reason=开始静默观察",
                    state.record_id,
                    state.source_name,
                    online_revision,
                )
                return

            if online_revision != state.pending_revision:
                self.store.save_pending(
                    state,
                    online_revision,
                    format_shanghai_time(now),
                )
                summary.observing += 1
                self.logger.info(
                    "Worker 表格检查完成：record_id=%s name=%s revision=%d "
                    "result=observing reason=表格再次变化，重新计时",
                    state.record_id,
                    state.source_name,
                    online_revision,
                )
                return

            pending_since = parse_state_time(state.pending_since)
            if pending_since is None:
                self.store.save_pending(
                    state,
                    online_revision,
                    format_shanghai_time(now),
                )
                summary.observing += 1
                self.logger.warning(
                    "Worker 表格检查完成：record_id=%s name=%s revision=%d "
                    "result=observing reason=观察时间无效，重新计时",
                    state.record_id,
                    state.source_name,
                    online_revision,
                )
                return

            quiet_duration = now - pending_since.astimezone(UTC)
            required_quiet = timedelta(minutes=self.config.change_quiet_minutes)
            if quiet_duration < required_quiet:
                summary.observing += 1
                self.logger.info(
                    "Worker 表格检查完成：record_id=%s name=%s revision=%d "
                    "result=observing reason=静默观察中 已稳定=%.1f分钟/%.1f分钟",
                    state.record_id,
                    state.source_name,
                    online_revision,
                    quiet_duration.total_seconds() / 60,
                    self.config.change_quiet_minutes,
                )
                return

            if not verify_monitor:
                ineligibility_reason = self._monitor_ineligibility_reason(state)
            if ineligibility_reason is not None:
                self.store.save_pending(state, None)
                summary.skipped += 1
                self.logger.info(
                    "Worker 表格检查完成：record_id=%s name=%s result=skipped reason=%s",
                    state.record_id,
                    state.source_name,
                    ineligibility_reason,
                )
                return

            self._copy_monitored_sheet(
                state,
                online_revision,
                metadata_title or state.source_name,
                summary,
            )
        except Exception as error:
            if _is_record_not_found_error(error):
                self._handle_deleted_record(state, summary)
                return
            summary.failed += 1
            message = str(error)
            self.logger.error(
                "Worker 表格检查完成：record_id=%s result=failed reason=%s",
                state.record_id,
                message,
            )
            self.logger.debug(
                "定时监控表格失败堆栈：record_id=%s",
                state.record_id,
                exc_info=True,
            )
            if state.pending_revision is not None:
                try:
                    self.store.save_pending(
                        state,
                        state.pending_revision,
                        format_shanghai_time(now),
                    )
                except Exception:
                    self.logger.debug("刷新观察重试时间失败", exc_info=True)
            if state.record_url and self.config.failure_notify_open_ids:
                card = build_sync_card(
                    original_name=state.source_name or "未知表格",
                    record_url=state.record_url,
                    status="failure",
                    sync_type="update",
                    target_folder_token=self.config.target_folder_token,
                    reason=message,
                )
                self._notify(list(self.config.failure_notify_open_ids), card)

    def _monitor_ineligibility_reason(self, state: SyncedSheetState) -> str | None:
        """
        功能说明：检查多维表记录是否仍符合监听条件，并返回具体跳过原因。

        参数：
            state：Redis 中记录 ID 与真实电子表格 token 的对应状态。

        返回值：
            符合监听条件时返回 None，否则返回便于日志排查的原因。
        """
        record = self.data_client.get_base_record(state.record_id)
        if not matches_required_fields(
            record.fields,
            self.config.monitor_required_fields,
        ):
            return "状态字段不符合监听条件"
        source = parse_source_sheet(record.fields.get(self.config.link_field_name))
        if source is None:
            return "表格链接为空或格式不支持"
        current_token = source.token
        if source.mention_type == "Wiki":
            current_token, document_type, _title = self.data_client.resolve_wiki_node(
                source.token
            )
            if document_type != "sheet":
                return f"链接对应的文档不是电子表格，而是 {document_type}"
        if current_token != state.source_token:
            return "记录中的表格链接已更换"
        return None

    def _copy_monitored_sheet(
        self,
        state: SyncedSheetState,
        revision: int,
        source_name: str,
        summary: SyncSummary,
    ) -> None:
        """
        功能说明：复制一张已经结束静默观察的电子表格并记录同步结果。

        参数：
            state：该表格最近一次成功同步的状态。
            revision：本次需要复制的在线 revision。
            source_name：当前源表格名称。
            summary：用于累计本轮复制结果的汇总对象。

        返回值：无。
        """
        if not state.record_url:
            raise RuntimeError("Redis 同步记录缺少原始记录链接")
        copy_version = self.store.next_copy_version(state.record_id, state.source_token)
        copy_name = build_copy_name(
            self.config.copy_name_prefix,
            source_name,
            copy_version,
        )
        self.logger.info(
            "Worker 开始搬运：record_id=%s name=%s revision=%d version=v%d "
            "target_name=%s",
            state.record_id,
            source_name,
            revision,
            copy_version,
            copy_name,
        )
        copied = self.data_client.copy_spreadsheet(state.source_token, copy_name)
        target_name = copied.name or copy_name
        self.store.save_synced(
            SyncedRecord(
                record_id=state.record_id,
                source_token=state.source_token,
                source_revision=revision,
                source_name=source_name,
                source_url=state.source_url,
                record_url=state.record_url,
                target_name=target_name,
                target_url=copied.url,
                synced_at=format_shanghai_time(self._now()),
                copy_version=copy_version,
                monitor_started_at=state.monitor_started_at,
                monitor_expires_at=state.monitor_expires_at,
            )
        )
        card = build_sync_card(
            original_name=source_name,
            record_url=state.record_url,
            target_name=target_name,
            target_url=copied.url,
            status="success",
            sync_type="update",
            target_folder_token=self.config.target_folder_token,
        )
        failed_count = self._notify(list(self.config.notify_open_ids), card)
        summary.copied += 1
        self.logger.info(
            "Worker 表格检查完成：record_id=%s revision=%d version=v%d "
            "target_name=%s result=copied "
            "failed_notification_count=%d",
            state.record_id,
            revision,
            copy_version,
            target_name,
            failed_count,
        )

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

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
                self.logger.debug("同步通知发送成功：open_id=%s", open_id)
            except Exception as error:
                failed_count += 1
                self.logger.error(
                    "同步通知发送失败：open_id=%s reason=%s",
                    open_id,
                    error,
                )
                self.logger.debug(
                    "同步通知发送失败堆栈：open_id=%s",
                    open_id,
                    exc_info=True,
                )
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


def build_copy_name(prefix: str, source_name: str, copy_version: int) -> str:
    """
    功能说明：按照搬运次数生成目标文件名，并将版本号放在表格扩展名前。

    参数：
        prefix：目标文件名前缀。
        source_name：源文件标题。
        copy_version：本次搬运的业务版本号，首次搬运为 1。

    返回值：
        首次搬运不带版本后缀，后续搬运带有 -vN 的目标文件名。
    """
    base_name = f"{prefix}{source_name}"
    if copy_version <= 1:
        return base_name

    matched = re.match(
        r"^(.*?)(\.(?:xlsx|xlsm|xlsb|xls|csv|ods))$",
        base_name,
        flags=re.IGNORECASE,
    )
    if matched is None:
        return f"{base_name}-v{copy_version}"
    stem, extension = matched.groups()
    return f"{stem}-v{copy_version}{extension}"


def matches_required_fields(fields: dict[str, Any], required: dict[str, Any]) -> bool:
    return all(
        _normalize_comparable(fields.get(name)) == _normalize_comparable(expected)
        for name, expected in required.items()
    )


def _finalize_summary_result(summary: SyncSummary) -> None:
    """根据本轮累计数量生成统一的主结果。"""
    if summary.failed:
        summary.result = "failed"
        summary.reason = "存在处理失败的表格"
    elif summary.copied:
        summary.result = "copied"
        summary.reason = ""
    elif summary.observing:
        summary.result = "observing"
        summary.reason = "存在等待静默期结束的表格"
    elif summary.skipped:
        summary.result = "skipped"
        summary.reason = "存在不符合监听条件的表格"
    else:
        summary.result = "unchanged"
        summary.reason = "没有需要搬运的新版本"


def _is_record_not_found_error(error: Exception) -> bool:
    """判断飞书多维表错误是否表示原始记录已经删除。"""
    return isinstance(error, FeishuApiError) and "RecordIdNotFound" in str(error)


def format_shanghai_time(value: datetime) -> str:
    shanghai_timezone = timezone(timedelta(hours=8))
    return value.astimezone(shanghai_timezone).isoformat(timespec="seconds")


def parse_state_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


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

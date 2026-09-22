from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Protocol

from stocking_sheet_sync.domain.models import (
    BaseRecord,
    CopyResult,
    CopyState,
    FillState,
    SourceSheet,
    SyncSummary,
)
from stocking_sheet_sync.infrastructure.feishu.client import CopyRejected
from stocking_sheet_sync.infrastructure.lease import RunLease, assert_run_lock
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from stocking_sheet_sync.services.notification import build_sync_card
from stocking_sheet_sync.settings import AppConfig


class DataClient(Protocol):
    def get_base_record(self, record_id: str) -> BaseRecord: ...
    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]: ...
    def copy_spreadsheet(
        self, spreadsheet_token: str, copy_name: str, *, folder_token: str | None = None
    ) -> CopyResult: ...
    def rename_spreadsheet(self, spreadsheet_token: str, title: str) -> None: ...


class MessageClient(Protocol):
    def send_card(self, open_id: str, card: dict[str, Any]) -> None: ...


class SyncBusyError(RuntimeError):
    """已有搬运任务占用运行锁。"""


class SyncService:
    def __init__(
        self,
        config: AppConfig,
        data_client: DataClient,
        message_client: MessageClient,
        store: RedisStateStore,
        logger: logging.Logger | None = None,
        *,
        now_provider: Callable[[], datetime] | None = None,
        history_filler=None,
        forecast_filler=None,
    ) -> None:
        """
        功能说明：组装一次性搬运服务及其依赖。

        参数：
            config：源多维表、目标文件夹、去重锁及通知配置。
            data_client：读取多维表和复制表格的数据客户端。
            message_client：发送飞书通知的消息客户端。
            store：保存搬运状态和并发锁的 Redis 存储。
            logger：可选日志记录器。
            now_provider：可选时间提供函数，默认使用当前 UTC 时间。
            history_filler：可选历史填充流程，接收副本记录与执行占位。
            forecast_filler：可选公式预估填充流程，接收同一副本与执行占位。

        返回值：无。
        """
        self.config = config
        self.data_client = data_client
        self.message_client = message_client
        self.store = store
        self.logger = logger or logging.getLogger(__name__)
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self.history_filler = history_filler
        self.forecast_filler = forecast_filler

    def run_record(
        self, record_id: str, *, force: bool = False, request_id: str = ""
    ) -> SyncSummary:
        """
        功能说明：处理一次搬运填充，普通请求按源表去重，强制请求按独立批次去重。

        参数：
            record_id：本次多维表自动化触发的记录 ID。
            force：是否创建独立强制批次，仍执行正常条件检查与填充流程。
            request_id：强制批次的请求标识，重试时保持相同，再次强制时更换。

        返回值：搬运、重复、跳过或失败的结果汇总；锁被占用时抛出 SyncBusyError。
        """
        validate_run_options(force, request_id)
        self.logger.debug("搬运参数：force=%s request_id=%s", force, request_id)
        lock = self.store.acquire_run_lock(self.config.lock_ttl_seconds)
        if lock is None:
            self.logger.info("搬运锁被占用：record_id=%s", record_id)
            raise SyncBusyError("已有同步任务正在运行")
        started = monotonic()
        batch = request_id or record_id
        self.logger.info("========== 任务开始 [%s] 记录=%s ==========", batch, record_id)
        lease = RunLease(self.store, lock, self.config.lock_ttl_seconds)
        summary = SyncSummary(scanned=1, force=force, request_id=request_id)
        record_url = ""
        source_name = "未知表格"
        current = None
        try:
            lease.start()
            record = self.data_client.get_base_record(record_id)
            record_url = record.shared_url
            if not matches_required_fields(record.fields, self.config.required_fields):
                summary.skipped = 1
                summary.result, summary.reason = "skipped", "状态字段不符合搬运条件"
                return summary
            source = parse_source_sheet(record.fields.get(self.config.link_field_name))
            if source is None:
                summary.skipped = 1
                summary.result, summary.reason = "skipped", "表格链接为空或格式不支持"
                return summary
            source_token, source_name = source.token, source.title
            if source.mention_type == "Wiki":
                source_token, document_type, title = self.data_client.resolve_wiki_node(
                    source.token
                )
                if document_type != "sheet":
                    raise RuntimeError(f"链接对应的文档不是电子表格，而是 {document_type}")
                source_name = title or source_name
            current = (
                self.store.get_state(record_id, source_token, request_id=request_id)
                if force
                else self.store.get_state(record_id, source_token)
            )
            if current is not None:
                if current.workflow == "triple":
                    from stocking_sheet_sync.services.copies import run_three_copy

                    return run_three_copy(self, current, summary)
                if current.status == "copied":
                    summary.unchanged = 1
                    summary.result, summary.reason = "unchanged", "源表格已搬运"
                    active = self._fill_copy(current, summary)
                    if active:
                        self._notify_result(current, summary)
                    return summary
                raise RuntimeError("该源表格已有未确认的搬运，请核对目标文件夹及 Redis 状态")
            if not record_url:
                raise RuntimeError("多维表记录缺少原始记录链接")
            batch_id = uuid.uuid4().hex
            started_at = self._now_text()
            batch_time = datetime.fromisoformat(started_at).strftime("%Y%m%d-%H%M%S")
            state = CopyState(
                record_id=record_id,
                source_token=source_token,
                source_name=source_name,
                source_url=source.source_url,
                record_url=record_url,
                status="copying",
                attempt_id=batch_id,
                started_at=started_at,
                request_id=request_id,
                workflow="triple" if self.config.backup_folder_token else "single",
                backup_folder_token=self.config.backup_folder_token,
                delivery_folder_token=self.config.target_folder_token,
                history_enabled=self.config.fill_history_enabled,
                forecast_enabled=self.config.fill_forecast_enabled,
                new_history_enabled=self.config.fill_new_history_enabled,
                new_forecast_enabled=self.config.fill_new_forecast_enabled,
                target_name=f"{self.config.copy_name_prefix}{source_name}",
                backup_name=f"{source_name}-{batch_time}",
            )
            if not self.store.begin_copy(state):
                raise RuntimeError("该源表格已被其他任务接管，请重新检查搬运状态")
            if state.workflow == "triple":
                from stocking_sheet_sync.services.copies import run_three_copy

                current = state
                return run_three_copy(self, state, summary)
            copy_name = f"{self.config.copy_name_prefix}{source_name}"
            self.logger.info("开始复制表格：record_id=%s target_name=%s", record_id, copy_name)
            try:
                copied = self.data_client.copy_spreadsheet(source_token, copy_name)
            except CopyRejected:
                self.store.cancel_copy(state)
                raise
            self.logger.info(
                "副本已生成：record_id=%s target_token=%s target_url=%s",
                record_id,
                copied.token,
                copied.url,
            )
            current = self.store.finish_copy(state, copied, self._now_text())
            summary.copied = 1
            summary.result = "copied"
            self._fill_copy(current, summary)
            self._notify_result(current, summary)
            return summary
        except Exception as error:
            summary.failed = 1
            summary.result, summary.reason = "failed", str(error)
            self.logger.exception("搬运处理失败：record_id=%s", record_id)
            if current is not None and current.status == "copied" and current.workflow == "single":
                summary.target_url = current.target_url
                self._degrade_fill(summary, str(error))
                if self.config.fill_history_enabled and summary.history_status == "disabled":
                    summary.history_status = "needs_review"
                if self.config.fill_forecast_enabled or self.config.fill_new_forecast_enabled:
                    summary.forecast_status = "needs_review"
                self._notify_result(current, summary)
                return summary
            if record_url and self.config.failure_notify_open_ids:
                try:
                    card = build_sync_card(
                        original_name=source_name,
                        record_url=record_url,
                        status="failure",
                        target_folder_token=self.config.target_folder_token,
                        reason=str(error),
                        original_backup_url=summary.original_backup_url,
                        filled_backup_url=summary.filled_backup_url,
                    )
                    self._notify(self.config.failure_notify_open_ids, card)
                except Exception:
                    self.logger.exception("生成失败通知卡片失败：record_id=%s", record_id)
            return summary
        finally:
            try:
                from stocking_sheet_sync.infrastructure.artifacts import cleanup_temp_files

                assert_run_lock()
                cleanup_temp_files(
                    Path(self.config.fill_report_dir),
                    self.config.temp_max_files,
                    self.config.temp_max_bytes,
                )
            except Exception:
                self.logger.exception("临时文件清理失败，保留搬运结果")
            lease.close()
            try:
                self.store.release_run_lock(lock)
            except Exception:
                self.logger.exception("释放搬运锁失败，等待锁自动过期：record_id=%s", record_id)
            if summary.result == "failed":
                self.logger.error("搬运失败：record_id=%s 原因：%s", record_id, summary.reason)
            elif summary.result == "skipped":
                self.logger.info("跳过搬运：record_id=%s 原因：%s", record_id, summary.reason)
            else:
                outcome = "复用已有交付" if summary.result == "unchanged" else "已交付"
                content = "未填充的原表副本" if summary.fill_degraded else "表格"
                self.logger.info("搬运完成：%s%s，链接：%s", outcome, content, summary.target_url)
            self.logger.info(
                "========== 任务结束 [%s] 耗时%.1f秒 ==========", batch, monotonic() - started
            )

    def _fill_copy(self, state: CopyState, summary: SyncSummary) -> bool:
        """
        功能说明：分别执行环境变量启用的填充阶段，保留已创建的副本和永久去重记录。

        参数：
            state：已确认创建成功的副本记录。
            summary：本次结果汇总，原位补充阶段状态、目标链接与报告路径。
        返回值：本次是否实际启动填充；公式预估与辅助历史量共享执行占位。
        """
        summary.target_url = state.target_url
        active = False
        reasons = []
        if (
            self.config.fill_history_enabled
            or self.config.fill_forecast_enabled
            or self.config.fill_new_history_enabled
            or self.config.fill_new_forecast_enabled
        ):
            previous = self.store.get_fill(state)
            mode_changed = previous is not None and (
                previous.history_enabled != self.config.fill_history_enabled
                or previous.forecast_enabled != self.config.fill_forecast_enabled
                or previous.new_history_enabled != self.config.fill_new_history_enabled
                or previous.new_forecast_enabled != self.config.fill_new_forecast_enabled
            )
            if mode_changed:
                stage_status = "needs_review"
                summary.fill_report_path = previous.report_path
                reasons.append("已有填充记录的开关组合不同，请使用手动重搬创建新批次")
            elif previous is not None and previous.status != "retryable":
                stage_status = previous.status
                summary.fill_report_path = previous.report_path
                if previous.status != "completed":
                    reasons.append(previous.reason or "填充已有执行占位，请核对报告后处理")
            else:
                as_of = (
                    previous.as_of
                    if previous
                    else datetime.fromisoformat(state.copied_at)
                    .astimezone(timezone(timedelta(hours=8)))
                    .date()
                    .isoformat()
                )
                attempt_id = uuid.uuid4().hex
                claim = FillState(
                    state.record_id,
                    state.source_token,
                    state.target_token,
                    as_of,
                    attempt_id,
                    report_path=str(Path(self.config.fill_report_dir) / attempt_id),
                    history_enabled=self.config.fill_history_enabled,
                    forecast_enabled=self.config.fill_forecast_enabled,
                    new_history_enabled=self.config.fill_new_history_enabled,
                    new_forecast_enabled=self.config.fill_new_forecast_enabled,
                )
                if not self.store.begin_fill(state, claim):
                    raise RuntimeError("填充已由其他任务接管，请核对状态")
                active = True
                stage_status = "running"
                summary.fill_report_path = claim.report_path
                try:
                    if self.config.fill_forecast_enabled or self.config.fill_new_forecast_enabled:
                        if self.forecast_filler is None:
                            from stocking_sheet_sync.services.fill import ForecastFiller

                            self.forecast_filler = ForecastFiller(
                                self.data_client,
                                config_path=Path(self.config.config_path),
                                history=self.config.fill_history_enabled,
                                legacy_forecast=self.config.fill_forecast_enabled,
                                new_forecast=self.config.fill_new_forecast_enabled,
                                new_history=self.config.fill_new_history_enabled,
                            )
                        result = self.forecast_filler(state, claim)
                    else:
                        if self.history_filler is None:
                            from stocking_sheet_sync.services.fill import HistoryFiller

                            self.history_filler = HistoryFiller(
                                self.data_client,
                                config_path=Path(self.config.config_path),
                                new_history=self.config.fill_new_history_enabled,
                                legacy_history=self.config.fill_history_enabled,
                            )
                        result = self.history_filler(state, claim)
                    completed = replace(
                        claim,
                        status=result["status"],
                        reason=result.get("reason", ""),
                        history_status=result.get("history_status", ""),
                        forecast_status=result.get("forecast_status", ""),
                        notification_details=result.get("notification_details", {}),
                    )
                    if completed.status not in {"completed", "retryable", "needs_review"}:
                        raise ValueError("填充返回了未知状态")
                except Exception as error:
                    self.logger.exception("填充执行异常，保留占位及副本")
                    completed = replace(claim, status="needs_review", reason=str(error))
                self.store.finish_fill(state, claim, completed)
                stage_status = completed.status
                if completed.status != "completed":
                    reasons.append(completed.reason or "填充需要核验")
            details = self.store.get_fill(state)
            if details and details.status == "completed":
                summary.notification_details = details.notification_details
            if self.config.fill_history_enabled or self.config.fill_new_history_enabled:
                summary.history_status = (
                    details.history_status if details and details.status == "completed" else ""
                ) or stage_status
            if self.config.fill_forecast_enabled or self.config.fill_new_forecast_enabled:
                summary.forecast_status = (
                    details.forecast_status if details and details.status == "completed" else ""
                ) or stage_status
        if reasons:
            self._degrade_fill(summary, "；".join(reasons))
        self.logger.debug(
            "副本填充阶段状态：target=%s history=%s forecast=%s report=%s",
            state.target_token,
            summary.history_status,
            summary.forecast_status,
            summary.fill_report_path,
        )
        return active

    def _degrade_fill(self, summary: SyncSummary, reason: str) -> None:
        """
        功能说明：填充未完成时按搬运成功降级，保留阶段状态与执行证据。

        参数：
            summary：已确认副本存在的结果汇总，原位更新整体结果。
            reason：填充未完成的具体原因。
        返回值：无；不修改副本内容或 Redis 中的填充占位。
        """
        summary.fill_degraded = True
        summary.failed = 0
        summary.result = "copied" if summary.copied else "unchanged"
        summary.reason = "已降级为仅搬运，填充未完成：" + reason
        self.logger.warning("填充失败：%s", reason)

    def _notify_result(self, state: CopyState, summary: SyncSummary) -> None:
        """按 summary 发送 state 的阶段结果卡片，通知异常不会改变已保存的去重记录。"""
        try:
            card = build_sync_card(
                original_name=state.source_name,
                record_url=state.record_url,
                status="failure" if summary.failed else "success",
                target_folder_token=state.delivery_folder_token or self.config.target_folder_token,
                target_name=state.target_name,
                target_url=state.target_url,
                reason=summary.reason if summary.fill_degraded or summary.failed else "",
                history_status=summary.history_status,
                forecast_status=summary.forecast_status,
                details=summary.notification_details,
                degraded=summary.fill_degraded,
                original_backup_url=summary.original_backup_url,
                filled_backup_url=summary.filled_backup_url,
            )
            self._notify(
                self.config.failure_notify_open_ids
                if summary.failed
                else self.config.notify_open_ids,
                card,
            )
        except Exception:
            self.logger.exception("副本结果通知失败：record_id=%s", state.record_id)

    def _now_text(self) -> str:
        value = self._now_provider()
        return format_shanghai_time(value if value.tzinfo else value.replace(tzinfo=UTC))

    def _notify(self, open_ids: tuple[str, ...], card: dict[str, Any]) -> None:
        for open_id in dict.fromkeys(open_ids):
            try:
                self.message_client.send_card(open_id, card)
                self.logger.debug("搬运通知发送成功：open_id=%s", open_id)
            except Exception:
                self.logger.exception("搬运通知发送失败：open_id=%s", open_id)


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


def validate_run_options(force: bool, request_id: str) -> None:
    """
    功能说明：校验强制执行参数，保证网络重试能定位同一批次。

    参数：
        force：必须为布尔值，表示是否强制创建新批次。
        request_id：强制批次标识，支持 1 至 128 位字母、数字、下划线或短横线。
    返回值：无；强制请求缺少标识或普通请求携带标识时抛出 ValueError。
    """
    if type(force) is not bool:
        raise ValueError("force 必须是 JSON 布尔值 true 或 false")
    if not isinstance(request_id, str):
        raise ValueError("request_id 必须是字符串")
    if force:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise ValueError("force=true 时必须提供有效的 request_id，重试时保持相同")
    elif request_id:
        raise ValueError("request_id 仅用于 force=true 的强制请求")

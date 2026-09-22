from __future__ import annotations

import logging
import uuid
from dataclasses import replace

from stocking_sheet_sync.domain.models import CopyResult, CopyState, CopyStep, SyncSummary
from stocking_sheet_sync.infrastructure.feishu.client import CopyRejected

LOG = logging.getLogger(__name__)


def copy_step(
    service, state: CopyState, stage: str, source: str, folder: str, name: str
) -> CopyStep:
    """
    功能说明：创建或复用一个已持久化的复制步骤，明确拒绝可重试，未知结果保留占位。

    参数：
        service：提供数据应用、状态存储及时间的搬运服务。
        state：所属批次。
        stage：original、filled 或 delivery 阶段。
        source：本阶段要复制的文件 token。
        folder：本阶段目标文件夹 token。
        name：新副本名称。
    返回值：复制成功的步骤；未确认的步骤抛错并停止，不自动再次复制。
    """
    previous = service.store.get_step(state, stage)
    if previous is not None:
        if previous.source_token != source or previous.folder_token != folder:
            raise ValueError("已保存步骤与本次复制来源或目标不一致")
        if previous.status != "copied":
            raise RuntimeError(f"{stage} 阶段已有未确认复制，请核对文件夹和状态")
        LOG.debug("复用复制步骤：stage=%s token=%s", stage, previous.target_token)
        return previous
    step = CopyStep(stage, source, folder, name, uuid.uuid4().hex)
    if not service.store.begin_step(state, step):
        raise RuntimeError(f"{stage} 阶段已由其他任务接管")
    LOG.debug("复制步骤开始：stage=%s source=%s folder=%s", stage, source, folder)
    try:
        result = service.data_client.copy_spreadsheet(source, name, folder_token=folder)
    except CopyRejected:
        service.store.cancel_step(state, step)
        raise
    completed = service.store.finish_step(state, step, result, service._now_text())
    LOG.info(
        "%s完成：%s",
        {"original": "原表备份", "filled": "处理副本", "delivery": "交付复制"}[stage],
        completed.target_url,
    )
    return completed


def run_three_copy(service, state: CopyState, summary: SyncSummary) -> SyncSummary:
    """
    功能说明：依次备份原表、填充处理备份，再交付通过核验的内容或原始备份。

    参数：
        service：共享锁、客户端、填充器及状态存储的搬运服务。
        state：包含冻结目录与填充开关的三份文件批次。
        summary：本次请求结果，原位补充各副本链接和填充状态。
    返回值：交付或复用结果；复制失败保留已完成阶段供重试，不将半成品交付。
    """
    from stocking_sheet_sync.services.sync import SyncService

    known = [service.store.get_step(state, stage) for stage in ("original", "filled", "delivery")]
    frozen = service.store.get_outcome(state)
    if (
        (known[1] and not known[0])
        or (known[2] and not known[1])
        or (frozen is not None and (not known[0] or not known[1]))
    ):
        raise ValueError("批次阶段记录存在缺口，请核对备份，不能重新创建前置文件")
    if state.status == "copied" and (not all(known) or frozen is None):
        raise ValueError("已交付批次记录不完整，停止创建或填充文件")
    if any(step and step.status != "copied" for step in known):
        raise RuntimeError("批次存在未确认的复制步骤，请核对实际文件")

    original = copy_step(
        service,
        state,
        "original",
        state.source_token,
        state.backup_folder_token,
        state.backup_name + "-原始备份" if state.backup_name else "原始备份-" + state.target_name,
    )
    summary.original_backup_url = original.target_url
    filled = copy_step(
        service,
        state,
        "filled",
        original.target_token,
        state.backup_folder_token,
        state.backup_name + "-填充未完成" if state.backup_name else "处理备份-" + state.target_name,
    )
    summary.filled_backup_url = filled.target_url
    outcome = service.store.get_outcome(state)
    if outcome is None:
        if state.status == "copied":
            raise ValueError("已交付批次缺少填充结果，停止补写")
        context = replace(
            state,
            status="copied",
            target_token=filled.target_token,
            target_name=filled.name,
            target_url=filled.target_url,
            copied_at=original.copied_at,
        )
        frozen_config = replace(
            service.config,
            fill_history_enabled=state.history_enabled,
            fill_new_history_enabled=state.new_history_enabled,
            fill_new_forecast_enabled=state.new_forecast_enabled,
            fill_forecast_enabled=state.forecast_enabled,
        )
        filler_service = SyncService(
            frozen_config,
            service.data_client,
            service.message_client,
            service.store,
            service.logger,
            now_provider=service._now_provider,
            history_filler=service.history_filler,
            forecast_filler=service.forecast_filler,
        )
        try:
            filler_service._fill_copy(context, summary)
        except Exception as error:
            # 协调状态必须可读；正在执行或未确认结束时，不和旧执行并发交付。
            pending = service.store.get_fill(context)
            if pending is not None and pending.status == "running":
                raise RuntimeError("填充执行结果尚未确认，保留原始备份并等待核验") from error
            LOG.exception("填充阶段异常，准备交付原始备份")
            summary.history_status = (
                "needs_review" if state.history_enabled or state.new_history_enabled else "disabled"
            )
            summary.forecast_status = (
                "needs_review"
                if state.forecast_enabled or state.new_forecast_enabled
                else "disabled"
            )
            service._degrade_fill(summary, str(error))
        finally:
            summary.target_url = ""
        if "running" in {summary.history_status, summary.forecast_status}:
            raise RuntimeError("填充正在执行或结果尚未确认，停止并发交付")
        outcome = service.store.save_outcome(
            state,
            {
                "history_status": summary.history_status,
                "forecast_status": summary.forecast_status,
                "fill_degraded": summary.fill_degraded,
                "reason": summary.reason,
                "fill_report_path": summary.fill_report_path,
                "delivery_source": "original" if summary.fill_degraded else "filled",
                "source_token": original.target_token
                if summary.fill_degraded
                else filled.target_token,
            },
        )
    expected = original if outcome["delivery_source"] == "original" else filled
    if outcome["source_token"] != expected.target_token:
        raise ValueError("交付决策与已确认的备份文件不一致")
    for key in (
        "history_status",
        "forecast_status",
        "fill_degraded",
        "reason",
        "fill_report_path",
        "delivery_source",
    ):
        setattr(summary, key, outcome[key])
    if state.backup_name and state.status != "copied":
        if not state.history_enabled and not state.forecast_enabled:
            suffix = "未填充"
        else:
            suffix = "填充未完成" if outcome["fill_degraded"] else "填充完成"
        final_name = f"{state.backup_name}-{suffix}"
        if filled.name != final_name:
            service.data_client.rename_spreadsheet(filled.target_token, final_name)
            filled = service.store.finish_step_rename(state, filled, final_name)
            LOG.debug("备份命名已保存：token=%s name=%s", filled.target_token, final_name)
    delivered = copy_step(
        service,
        state,
        "delivery",
        outcome["source_token"],
        state.delivery_folder_token,
        state.target_name,
    )
    summary.target_url = delivered.target_url
    if state.status == "copied":
        if state.target_token != delivered.target_token:
            raise ValueError("交付文件与批次成功记录不一致")
        summary.unchanged = 1
        summary.result = "unchanged"
        return summary
    result = CopyResult(delivered.name, delivered.target_token, "sheet", delivered.target_url)
    completed = service.store.finish_copy(state, result, delivered.copied_at)
    summary.copied = 1
    summary.result = "copied"
    summary.failed = 0
    service._notify_result(completed, summary)
    return summary

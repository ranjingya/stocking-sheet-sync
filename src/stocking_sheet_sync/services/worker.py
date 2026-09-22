"""串行处理已持久化任务，复用业务批次去重与恢复逻辑。"""

import logging
from dataclasses import asdict

from stocking_sheet_sync.infrastructure.lease import assert_run_lock
from stocking_sheet_sync.services.sync import SyncBusyError

LOG = logging.getLogger(__name__)


def process_one(queue, service, config) -> bool:
    """
    功能说明：消费一个任务，保存结果后确认，失败有限重试，锁忙不计入次数。

    参数：
        queue：持久化任务队列。
        service：现有搬运填充服务。
        config：重试次数等运行配置。
    返回值：需要等待后重试时为True，否则为False。
    """
    task = queue.take()
    if task is None:
        return False
    state = queue.status(task.task_id)
    if int(state.get("attempts", 0)) >= config.queue_max_attempts:
        LOG.error("任务停止重试：record_id=%s 已达次数上限，请人工核对", task.record_id)
        queue.finish(task, "failed", {"reason": "达到最大执行次数，请核对批次与在线文件后手动处理"})
        return False
    attempt = queue.begin(task)
    LOG.debug(
        "后台任务开始：task_id=%s record_id=%s attempt=%d", task.task_id, task.record_id, attempt
    )
    try:
        summary = service.run_record(
            task.record_id, force=True, request_id=f"webhook-{task.task_id}"
        )
    except SyncBusyError:
        queue.defer(task, "已有搬运任务占用运行锁", busy=True)
        return True
    except Exception as error:
        LOG.exception("后台执行异常：task_id=%s", task.task_id)
        result = {"reason": str(error)}
    else:
        assert_run_lock()
        result = asdict(summary)
        if summary.result != "failed":
            # 已按策略降级交付原表的任务同样算完成，不重新填充。
            queue.finish(task, "completed", result)
            return False
    if attempt >= config.queue_max_attempts:
        LOG.error("任务停止重试：record_id=%s 已达次数上限，请人工核对", task.record_id)
        queue.finish(task, "failed", result)
        return False
    LOG.warning(
        "任务将重试：record_id=%s 第%d/%d次", task.record_id, attempt + 1, config.queue_max_attempts
    )
    queue.defer(task, result.get("reason", "任务执行失败"))
    return True

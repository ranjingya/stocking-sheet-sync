"""后台线程串行消费Redis任务，并在连接异常后恢复。"""

import logging
from contextlib import ExitStack
from threading import Event

from stocking_sheet_sync.bootstrap import build_service
from stocking_sheet_sync.infrastructure.lease import RunLease, assert_run_lock
from stocking_sheet_sync.infrastructure.queue import TaskQueue
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from stocking_sheet_sync.services.worker import process_one

LOG = logging.getLogger(__name__)


def consume(config, stop: Event) -> None:
    """
    功能说明：后台串行处理队列，异常后重连；停止时完成当前任务后退出。

    参数：
        config：运行配置，包含Redis连接、锁和重试参数。
        stop：主进程发送的停止事件。
    返回值：无；退出时释放本轮连接与锁。
    """
    while not stop.is_set():
        try:
            consume_session(config, stop)
        except Exception:
            LOG.exception("后台处理异常，稍后重试，未完成任务保留")
            stop.wait(config.queue_retry_delay_seconds)


def consume_session(config, stop: Event) -> None:
    """
    功能说明：持有消费者独占锁，恢复并串行消费任务。

    参数：
        config：Redis和处理服务配置。
        stop：停止信号，当前任务结束后检查。
    返回值：无；连接或失锁异常交由外层重连。
    """
    with ExitStack() as resources:
        queue = TaskQueue(config)
        resources.callback(queue.close)
        owner = RedisStateStore(
            config.redis_url,
            config.redis_key_prefix + ":queue:worker",
            socket_timeout_seconds=config.request_timeout_seconds,
        )
        resources.callback(owner.close)
        token = None
        while not stop.is_set() and token is None:
            token = owner.acquire_run_lock(config.lock_ttl_seconds)
            if token is None:
                stop.wait(config.queue_retry_delay_seconds)
        if token is None:
            return
        resources.callback(owner.release_run_lock, token)
        lease = RunLease(owner, token, config.lock_ttl_seconds)
        lease.start()
        resources.callback(lease.close)
        service = build_service(config, resources, migrate=True)
        LOG.info("后台消费者启动，优先恢复未确认任务")
        while not stop.is_set():
            assert_run_lock()
            if process_one(queue, service, config):
                stop.wait(config.queue_retry_delay_seconds)
        LOG.info("后台消费者正常停止")

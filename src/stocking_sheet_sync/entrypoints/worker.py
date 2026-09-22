"""容器内独立后台进程，串行消费Redis任务。"""

import argparse
import logging
import signal
from contextlib import ExitStack
from threading import Event

from stocking_sheet_sync.bootstrap import build_service
from stocking_sheet_sync.infrastructure.lease import RunLease, assert_run_lock
from stocking_sheet_sync.infrastructure.queue import TaskQueue
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.worker import process_one
from stocking_sheet_sync.settings import load_config

LOG = logging.getLogger(__name__)


def run(argv=None) -> int:
    """
    功能说明：获得后台消费者独占锁，恢复未确认任务并循环消费；信号到达后完成当前任务退出。

    参数：
        argv：可选命令参数，用于显示帮助。
    返回值：正常停止为0；失锁、连接或初始化失败为1，由进程管理器重启。
    """
    argparse.ArgumentParser(description="串行消费下单需求任务").parse_args(argv)
    stop = Event()
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        config = load_config()
        configure_logging(config.log_level)
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
                return 0
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
        return 0
    except Exception:
        LOG.exception("后台消费者停止，未确认任务保留在Redis")
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main():
    raise SystemExit(run())

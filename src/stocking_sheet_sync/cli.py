from __future__ import annotations

import argparse
import logging
import signal
import threading
import time

from .config import load_config
from .lark_client import FeishuClient
from .logging_config import configure_logging
from .redis_store import RedisStateStore
from .sync_service import SyncService


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：初始化同步 Worker，并按单次或分级轮询模式运行。

    参数：
        argv：可选命令行参数；支持 --once。

    返回值：
        进程退出码，正常完成返回 0。
    """
    parser = argparse.ArgumentParser(description="飞书备货表格同步服务")
    parser.add_argument("--once", action="store_true", help="执行一轮扫描后退出")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("stocking_sheet_sync")
    store = RedisStateStore(
        config.redis_url,
        config.redis_key_prefix,
        monitor_days=config.monitor_days,
        socket_timeout_seconds=config.request_timeout_seconds,
        logger=logger,
    )
    data_client = FeishuClient(
        config,
        config.feishu_data_app_id,
        config.feishu_data_app_secret,
        "data",
        logger,
    )
    message_client = FeishuClient(
        config,
        config.feishu_message_app_id,
        config.feishu_message_app_secret,
        "message",
        logger,
    )
    service = SyncService(config, data_client, message_client, store, logger)
    stopping = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("收到退出信号，准备停止程序：signal=%d", signum)
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        next_full_check = 0.0
        while not stopping.is_set():
            now = time.monotonic()
            check_all = now >= next_full_check
            service.run_once(check_all=check_all)
            if check_all:
                next_full_check = time.monotonic() + config.poll_interval_minutes * 60
            if args.once:
                break
            logger.debug(
                "等待下一轮变动表格检查：interval_minutes=%.2f",
                config.change_check_interval_minutes,
            )
            stopping.wait(config.change_check_interval_minutes * 60)
        return 0
    finally:
        data_client.close()
        message_client.close()
        store.close()


def main() -> None:
    raise SystemExit(run())

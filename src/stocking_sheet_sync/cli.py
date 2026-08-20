from __future__ import annotations

import argparse
import logging
import signal
import threading

from .config import load_config
from .lark_client import FeishuClient
from .logging_config import configure_logging
from .redis_store import RedisStateStore
from .sync_service import SyncService


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：初始化同步服务，并按单次、基线或常驻模式运行。

    参数：
        argv：可选命令行参数；支持 --once 和 --baseline。

    返回值：
        进程退出码，正常完成返回 0。
    """
    parser = argparse.ArgumentParser(description="飞书备货表格同步服务")
    parser.add_argument("--once", action="store_true", help="执行一轮扫描后退出")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="仅为未接管记录建立当前 revision 基线，不复制文件",
    )
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("stocking_sheet_sync")
    store = RedisStateStore(
        config.redis_url,
        config.redis_key_prefix,
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
        while not stopping.is_set():
            service.run_once(baseline=args.baseline)
            if args.once or args.baseline:
                break
            logger.info(
                "等待下一轮产品下单同步扫描：interval_minutes=%.2f",
                config.poll_interval_minutes,
            )
            stopping.wait(config.poll_interval_minutes * 60)
        return 0
    finally:
        data_client.close()
        message_client.close()
        store.close()


def main() -> None:
    raise SystemExit(run())

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from contextlib import ExitStack
from dataclasses import asdict

from stocking_sheet_sync.infrastructure.feishu.client import FeishuClient
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.sync import SyncBusyError, SyncService, validate_run_options
from stocking_sheet_sync.settings import load_config

LOG = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：手动创建或恢复一次独立搬运填充批次，执行结束后关闭资源并退出。

    参数：
        argv：可选命令行参数；默认读取进程参数，包含记录 ID 及可选重试标识。
    返回值：搬运成功、填充降级或条件跳过返回 0，处理失败返回 1，参数错误为 2，忙碌为 3。
    """
    parser = argparse.ArgumentParser(description="手动重新搬运并填充一条多维表记录")
    parser.add_argument("--record-id", required=True, help="需要重新处理的多维表记录 ID")
    parser.add_argument("--request-id", help="重试已有批次时填写日志中的标识；省略则自动生成新批次")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", args.record_id):
        parser.error("record-id 格式无效")
    request_id = uuid.uuid4().hex if args.request_id is None else args.request_id
    try:
        validate_run_options(True, request_id)
    except ValueError as error:
        parser.error(str(error))
    configure_logging()
    LOG.info("手动搬运开始：record_id=%s request_id=%s", args.record_id, request_id)
    LOG.info(
        "重试本批次命令：uv run stocking-sheet-sync-rerun --record-id %s --request-id %s",
        args.record_id,
        request_id,
    )
    try:
        config = load_config()
        configure_logging(config.log_level)
        with ExitStack() as resources:
            store = RedisStateStore(
                config.redis_url,
                config.redis_key_prefix,
                socket_timeout_seconds=config.request_timeout_seconds,
            )
            resources.callback(store.close)
            data_client = FeishuClient(
                config, config.feishu_data_app_id, config.feishu_data_app_secret, "data"
            )
            resources.callback(data_client.close)
            message_client = FeishuClient(
                config, config.feishu_message_app_id, config.feishu_message_app_secret, "message"
            )
            resources.callback(message_client.close)
            service = SyncService(config, data_client, message_client, store)
            summary = service.run_record(args.record_id, force=True, request_id=request_id)
            sys.stdout.write(json.dumps(asdict(summary), ensure_ascii=False) + "\n")
            LOG.info(
                "手动搬运结束：result=%s target=%s history=%s forecast=%s reason=%s",
                summary.result,
                summary.target_url,
                summary.history_status,
                summary.forecast_status,
                summary.reason,
            )
            return 1 if summary.result == "failed" else 0
    except SyncBusyError:
        LOG.warning("已有任务正在运行；稍后使用相同 request-id 重试：%s", request_id)
        return 3
    except Exception:
        LOG.exception("手动搬运未完成；使用相同 request-id 核对或重试：%s", request_id)
        return 1


def main() -> None:
    raise SystemExit(run())

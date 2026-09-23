"""按用户指定链接原地填写市场部历史和预估，不发送通知。"""

import argparse
import logging
import re
import uuid
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from stocking_sheet_sync.bootstrap import build_service
from stocking_sheet_sync.domain.models import CopyState, FillState
from stocking_sheet_sync.infrastructure.artifacts import (
    cleanup_completed_report,
    cleanup_temp_files,
)
from stocking_sheet_sync.infrastructure.lease import RunLease, assert_run_lock
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.fill import ForecastFiller, HistoryFiller
from stocking_sheet_sync.settings import load_config

LOG = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：直接填写链接指定的表格，显式选择历史或预测，与自动任务共用运行锁。

    参数：
        argv：命令参数，包含url、history或forecast，以及可选as-of和sheet-id。
    返回值：完成返回0；异常返回1；部分完成或需人工处理返回2；运行锁忙返回3。
    """
    parser = argparse.ArgumentParser(description="原地填写市场部数据，不复制表格、不发送通知")
    parser.add_argument("--url", required=True, help="飞书电子表格或知识库表格链接")
    parser.add_argument("--history", action="store_true", help="填充历史销量")
    parser.add_argument("--forecast", action="store_true", help="填充老款预估，新品预测暂不支持")
    parser.add_argument("--as-of", type=date.fromisoformat, help="基准日，默认上海当天日期")
    parser.add_argument("--sheet-id", help="指定工作表；默认使用链接中的sheet参数或自动识别")
    args = parser.parse_args(argv)
    if not args.history and not args.forecast:
        parser.error("至少指定 --history 或 --forecast，可以同时指定")
    url = urlsplit(args.url)
    match = re.fullmatch(r"/(sheets|wiki)/([A-Za-z0-9]+)/*", url.path)
    if url.scheme != "https" or not url.hostname or not match:
        parser.error("请提供有效的HTTPS飞书表格或知识库链接")
    sid = args.sheet_id or parse_qs(url.query).get("sheet", [None])[0]
    if sid and not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        parser.error("工作表ID格式无效")
    as_of = args.as_of or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    attempt = uuid.uuid4().hex
    configure_logging()
    try:
        config = load_config()
        configure_logging(config.log_level)
        with ExitStack() as resources:
            service = build_service(config, resources)
            lock = service.store.acquire_run_lock(config.lock_ttl_seconds)
            if lock is None:
                LOG.warning("已有任务正在处理，请稍后重试")
                return 3
            resources.callback(service.store.release_run_lock, lock)
            lease = RunLease(service.store, lock, config.lock_ttl_seconds)
            resources.callback(lease.close)
            lease.start()
            token = match.group(2)
            if match.group(1) == "wiki":
                token, kind, _ = service.data_client.resolve_wiki_node(token)
                if kind != "sheet":
                    raise ValueError("知识库链接对应的文档不是电子表格")
            output = Path(config.fill_report_dir) / attempt
            copy = CopyState(
                "manual",
                token,
                "指定表格",
                args.url,
                args.url,
                "copied",
                target_token=token,
                target_url=args.url,
            )
            claim = FillState(
                "manual",
                token,
                token,
                as_of.isoformat(),
                attempt,
                report_path=str(output),
                history_enabled=args.history,
                forecast_enabled=args.forecast,
            )
            filler = (
                ForecastFiller(
                    service.data_client,
                    history=args.history,
                    new_history=args.history,
                    new_forecast=True,
                )
                if args.forecast
                else HistoryFiller(service.data_client)
            )
            if sid:
                filler.find_sheet = lambda *a: sid
            LOG.info(
                "========== 原地填充开始 [%s] 表格=%s 基准日=%s ==========",
                attempt,
                args.url,
                as_of,
            )
            result = filler(copy, claim)
            assert_run_lock()
            details = result.get("notification_details", {})
            partial = bool(details.get("blocked_platforms")) or any(
                details.get(key, {}).get("status") in {"partial", "manual"}
                for key in ("history", "forecast")
            )
            partial |= args.forecast and result.get("forecast_status") in {"unsupported", "skipped"}
            code = 0 if result["status"] == "completed" and not partial else 2
            LOG.info(
                "========== 原地填充结束 [%s] 结果=%s 原因=%s ==========",
                attempt,
                "完成" if code == 0 else "部分完成或需核对",
                result.get("reason")
                or "；".join(
                    dict.fromkeys(
                        reason
                        for key in ("history", "forecast")
                        for reason in details.get(key, {}).get("reasons", [])
                    )
                )
                or "无",
            )
            if code == 0:
                cleanup_completed_report(Path(config.fill_report_dir), output)
            else:
                LOG.warning("核对报告：%s", output)
            cleanup_temp_files(
                Path(config.fill_report_dir), config.temp_max_files, config.temp_max_bytes
            )
            return code
    except Exception:
        LOG.exception("原地填充失败，请核对日志和本次报告")
        return 1

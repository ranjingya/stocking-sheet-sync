from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

import httpx

from stocking_sheet_sync.infrastructure.feishu.sheets import read_sheet as read_sheet_api
from stocking_sheet_sync.infrastructure.warehouse import SalesReader
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.sales import inspect_sales, write_report
from stocking_sheet_sync.settings import WarehouseSettings, load_sales_config

LOG = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：读取线上表格或本地快照，检查数仓匹配并保存结果。

    参数：
        argv：可选命令行参数列表，默认读取进程参数。

    返回值：检查执行成功返回 0；读取或配置失败返回 1，业务异常在报告中列明。
    """
    parser = argparse.ArgumentParser(description="只读检查下单表商品匹配与历史发货数据")
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument("--db-env-file", type=Path, help="数仓凭证文件，默认读取当前目录 .env")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--spreadsheet-token")
    group.add_argument("--snapshot", type=Path)
    parser.add_argument("--sheet-id")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True, help="预估日 YYYY-MM-DD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.spreadsheet_token and not args.sheet_id:
        parser.error("读取线上表格必须同时提供 --sheet-id")
    configure_logging("INFO")
    try:
        config = load_sales_config(args.config)
        snapshot = (
            json.loads(args.snapshot.read_text(encoding="utf-8"))
            if args.snapshot
            else read_sheet_api(args.spreadsheet_token, args.sheet_id)
        )
        reader = SalesReader(WarehouseSettings.load(args.db_env_file))
        report = inspect_sales(reader, snapshot, config, args.as_of)
        write_report(args.output, report, snapshot)
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, httpx.TransportError) as error:
        LOG.error("检查未完成：%s", error)
        return 1


def main() -> None:
    raise SystemExit(run())

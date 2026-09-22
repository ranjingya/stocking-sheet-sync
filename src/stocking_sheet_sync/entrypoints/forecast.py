from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from stocking_sheet_sync.domain.products import inspect_sheet
from stocking_sheet_sync.infrastructure.warehouse import ForecastReader
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.calculation import (
    inspect_forecast,
    write_forecast_report,
)
from stocking_sheet_sync.settings import (
    WarehouseSettings,
    load_forecast_config,
    load_forecast_sources,
    load_layout_config,
    load_sales_config,
)

LOG = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：执行独立的整款只读数仓试算并生成本地JSON和CSV。

    参数：
        argv：命令行参数；空值时读取进程参数。
    返回值：全部可计算返回0，存在待核对返回2，运行或配置失败返回1。
    """
    parser = argparse.ArgumentParser(description="只读试算老款需求，不修改飞书表格")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--style", action="append", help="按款号诊断，可重复传入")
    scope.add_argument("--snapshot", type=Path, help="按本地下单表快照中的SKU试算")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument("--db-env-file", type=Path)
    args = parser.parse_args(argv)
    configure_logging("INFO")
    try:
        config = load_sales_config(args.config)
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
        requested_rows = inspect_sheet(snapshot, config)["rows"] if snapshot else None
        report = inspect_forecast(
            ForecastReader(WarehouseSettings.load(args.db_env_file)),
            args.style or [],
            args.as_of,
            config,
            load_forecast_sources(args.config),
            load_forecast_config(args.config),
            load_layout_config(args.config, config),
            requested_rows=requested_rows,
            snapshot=snapshot,
        )
        write_forecast_report(args.output, report)
        return (
            2
            if report["summary"]["styles_needing_review"] or report["summary"]["styles_manual"]
            else 0
        )
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as error:
        LOG.error("预测试算未完成：%s", error)
        return 1


def main() -> None:
    raise SystemExit(run())

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import httpx

from stocking_sheet_sync.domain.sheets.validation import verify_sales_update
from stocking_sheet_sync.domain.sheets.values import build_sales_update
from stocking_sheet_sync.infrastructure.feishu.sheets import (
    current_revision,
    read_sheet,
    write_sales_ranges,
)
from stocking_sheet_sync.infrastructure.warehouse import SalesReader
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.sales import inspect_sales, write_report
from stocking_sheet_sync.settings import WarehouseSettings, load_sales_config

LOG = logging.getLogger(__name__)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：读取数仓并预览或填充平台近30天销量，保存证据并回读校验。

    参数：
        argv：命令行参数列表；默认读取进程参数。

    返回值：预览或核验通过返回0，运行失败返回1，存在待核对项返回2。
    """
    parser = argparse.ArgumentParser(description="预览或填充市场部近30天发货销量")
    parser.add_argument("--spreadsheet-token", required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"))
    parser.add_argument("--db-env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    if args.apply and args.expected_revision is None:
        parser.error("执行必须指定 --expected-revision")
    configure_logging("INFO")

    def save(name, data):
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        LOG.info(
            "开始销量填充：sheet_id=%s start=%s end=%s apply=%s",
            args.sheet_id,
            args.as_of - timedelta(days=30),
            args.as_of - timedelta(days=1),
            args.apply,
        )
        config = load_sales_config(args.config)
        before = read_sheet(
            args.spreadsheet_token,
            args.sheet_id,
            include_style=True,
            archive_path=args.output / "before.xlsx",
        )
        save("before.json", before)
        if args.apply and before["revision"] != args.expected_revision:
            raise ValueError("表格版本与指定版本不一致，请重新预览")
        reader = SalesReader(WarehouseSettings.load(args.db_env_file))
        report = inspect_sales(reader, before, config, args.as_of)
        write_report(args.output, report, before)
        update = build_sales_update(before, report, config)
        save("request.json", update)
        if update["status"] == "needs_review":
            save("result.json", {"status": "needs_review", **update["summary"]})
            LOG.warning("销量存在待核对项，本次不写入：count=%d", update["summary"]["needs_review"])
            return 2
        if not update["operations"]:
            result = verify_sales_update(before, before, update)
            save(
                "result.json",
                {
                    **result,
                    "status": "unchanged",
                    "revision": before["revision"],
                    **update["summary"],
                },
            )
            LOG.info("销量及平台合计与数仓一致，无需写入")
            return 0
        save("payload.json", {"valueRanges": update["operations"]})
        if not args.apply:
            save(
                "result.json",
                {"status": "preview", "revision": before["revision"], **update["summary"]},
            )
            LOG.info("销量预览完成：output=%s", args.output)
            return 0
        current = current_revision(args.spreadsheet_token, args.sheet_id)
        if current != before["revision"]:
            raise ValueError("提交前表格版本发生变化，请重新预览")
        save(
            "response.json",
            write_sales_ranges(
                args.spreadsheet_token,
                args.sheet_id,
                update["operations"],
                expected_revision=before["revision"],
            ),
        )
        after = read_sheet(
            args.spreadsheet_token,
            args.sheet_id,
            include_style=True,
            archive_path=args.output / "after.xlsx",
        )
        save("after.json", after)
        result = verify_sales_update(before, after, update)
        save("result.json", result)
        LOG.info(
            "销量填充完成并通过回读：sales_cells=%d totals=%s",
            result["checked_sales_cells"],
            result["platform_totals"],
        )
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, httpx.TransportError) as error:
        LOG.error("销量填充未完成：%s", error)
        save("error.json", {"error": str(error)})
        return 1


def main() -> None:
    raise SystemExit(run())

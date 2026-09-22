from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import httpx

from stocking_sheet_sync.domain.products import inspect_sheet, match_catalog
from stocking_sheet_sync.infrastructure.feishu.sheets import read_sheet as read_sheet_api
from stocking_sheet_sync.infrastructure.warehouse import SalesReader
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.source_settings import WarehouseSettings, load_sales_config

LOG = logging.getLogger(__name__)


def inspect_sales(reader: SalesReader, snapshot: dict, config: dict, as_of: date) -> dict:
    """
    功能说明：联合表格、主数据与平台发货数据生成可审阅的匹配检查结果。

    参数：
        reader：只读数仓客户端。
        snapshot：完整工作表快照。
        config：平台和字段映射配置。
        as_of：本次预估日，用于确定最近 30 个完整自然日。

    返回值：商品与列映射、来源覆盖以及逐单元格候选结果；不执行写入。
    """
    layout = inspect_sheet(snapshot, config)
    skus = sorted({row["sku"] for row in layout["rows"] if row["sku"]})
    catalog = reader.catalog(config["catalog"], skus)
    match_catalog(layout, catalog)
    sources, entries = [], []
    for platform in config["platforms"]:
        try:
            source = reader.sales(platform, skus, as_of)
        except (RuntimeError, ValueError) as error:
            LOG.error("平台读取失败：platform=%s reason=%s", platform["id"], error)
            source = {
                "platform": platform["id"],
                "source_table": platform["table"],
                "rows": [],
                "issues": ["source_read_failed"],
                "error": str(error),
            }
        sources.append(source)
        by_sku = defaultdict(list)
        for item in source["rows"]:
            by_sku[item["sku"]].append(item)
        col = layout["columns"][platform["id"]]["sales"]
        for row in layout["rows"]:
            problems = [*row["issues"], *source["issues"]]
            if not col:
                problems.append("sales_column_unresolved")
            items = by_sku[row["sku"]]
            quantity, evidence = None, "missing"
            if len(items) > 1:
                problems.append("duplicate_source_sku")
            elif items:
                quantity = items[0]["quantity"]
                evidence = items[0]["status"]
                if evidence != "matched":
                    problems.append(evidence)
            elif platform["kind"] == "detail" and not source["issues"] and not row["issues"]:
                quantity, evidence = 0, "no_shipments_in_observed_window"
            else:
                problems.append("sku_sales_missing")
            target = f"{col}{row['row']}" if col else None
            cell = snapshot["cells"].get(target, {})
            existing = cell.get("value")
            if cell.get("formula") or existing not in (None, ""):
                problems.append("target_not_empty")
            entry = {
                "platform": platform["id"],
                "platform_name": platform["name"],
                "row": row["row"],
                "sku": row["sku"],
                "style": row["style"],
                "name": row["name"],
                "spec": row["spec"],
                "target_cell": target,
                "observed_quantity": quantity,
                "candidate_quantity": quantity if not problems else None,
                "existing_value": existing,
                "evidence": evidence,
                "status": "ready" if not problems else "needs_review",
                "issues": sorted(set(problems)),
            }
            entries.append(entry)
    counts = Counter(item["status"] for item in entries)
    return {
        "as_of": as_of.isoformat(),
        "spreadsheet_token": snapshot.get("spreadsheet_token"),
        "layout": layout,
        "sources": sources,
        "entries": entries,
        "summary": {
            "sku_rows": len(layout["rows"]),
            "catalog_matched": sum(row["match_status"] == "matched" for row in layout["rows"]),
            "platform_count": len(config["platforms"]),
            "entry_count": len(entries),
            **counts,
        },
    }


def write_report(output: Path, report: dict, snapshot: dict) -> None:
    """将 report 和 snapshot 保存到 output 目录，输出 JSON 与逐行 CSV，无返回值。"""
    output.mkdir(parents=True, exist_ok=True)
    for name, value in (("inspection.json", report), ("sheet-snapshot.json", snapshot)):
        (output / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    if report["entries"]:
        with (output / "matching.csv").open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(report["entries"][0]))
            writer.writeheader()
            for row in report["entries"]:
                writer.writerow({**row, "issues": ";".join(row["issues"])})
    LOG.info("本地检查结果已保存：output=%s summary=%s", output, report["summary"])


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：读取线上表格或本地快照，检查数仓匹配并保存结果。

    参数：
        argv：可选命令行参数列表，默认读取进程参数。

    返回值：检查执行成功返回 0；读取或配置失败返回 1，业务异常在报告中列明。
    """
    parser = argparse.ArgumentParser(description="只读检查下单表商品匹配与历史发货数据")
    parser.add_argument("--source-config", type=Path, default=Path("config/sales-sources.toml"))
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
        config = load_sales_config(args.source_config)
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

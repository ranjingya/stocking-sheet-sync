from __future__ import annotations

import argparse
import csv
import json
import logging
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .logging_config import configure_logging
from .sales_config import WarehouseSettings, load_sales_config
from .sales_reader import SalesReader
from .sheet_matching import cells_from_envelope, column_name, inspect_sheet, match_catalog

LOG = logging.getLogger(__name__)


def _lark_read(command: str, args: list[str]) -> dict:
    """使用用户身份执行受限的飞书只读 command 和 args，返回成功 JSON。"""
    if command not in {"+workbook-info", "+cells-get", "+sheet-info"}:
        raise ValueError("检查工具只支持飞书读取命令")
    result = subprocess.run(
        ["lark-cli", "sheets", command, "--as", "user", *args],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        raise RuntimeError("飞书读取失败，请检查 lark-cli 用户登录态和表格访问权限")
    payload = json.loads(result.stdout)
    if payload.get("ok") is not True:
        raise RuntimeError("飞书读取未返回成功状态")
    return payload


def read_sheet(token: str, sheet_id: str) -> dict:
    """
    功能说明：读取完整工作表值、公式和合并范围，校验分页及读取期间版本一致。

    参数：
        token：飞书电子表格 token。
        sheet_id：需要检查的工作表 ID。

    返回值：带完整坐标的本地快照，含原始数据和表格身份。
    """
    locator = ["--spreadsheet-token", token]
    book = _lark_read("+workbook-info", locator)["data"]
    sheet = next((s for s in book["sheets"] if s["sheet_id"] == sheet_id), None)
    if sheet is None:
        raise ValueError("指定工作表不存在，请检查 sheet_id")
    rows, cols = sheet["row_count"], sheet["column_count"]
    if not 1 <= rows <= 50000 or not 1 <= cols <= 1000:
        raise ValueError("工作表尺寸超出检查工具支持范围")
    locator += ["--sheet-id", sheet_id]
    layout = _lark_read("+sheet-info", [*locator, "--include", "merges"])["data"]
    cells = {}
    chunk = max(1, min(200, 8000 // cols))
    for start in range(1, rows + 1, chunk):
        stop = min(rows, start + chunk - 1)
        block = _lark_read(
            "+cells-get",
            [
                *locator,
                "--range",
                f"A{start}:{column_name(cols)}{stop}",
                "--include",
                "value,formula",
                "--max-chars",
                "500000",
            ],
        )
        actual = cells_from_envelope(block)
        expected = {
            f"{column_name(c)}{r}" for r in range(start, stop + 1) for c in range(1, cols + 1)
        }
        if actual.keys() != expected:
            raise ValueError("工作表分页读取范围不完整")
        cells.update(actual)
        LOG.info("工作表读取完成分页：sheet_id=%s start=%d end=%d", sheet_id, start, stop)
    after = _lark_read("+workbook-info", ["--spreadsheet-token", token])["data"]
    if book["revision"] != after["revision"]:
        raise RuntimeError("读取期间表格有修改，请重新运行检查")
    return {
        "spreadsheet_token": token,
        "title": book["title"],
        "sheet_id": sheet_id,
        "row_count": rows,
        "column_count": cols,
        "revision": book["revision"],
        "cells": cells,
        "merges": [item["range"] for item in layout["merged_cells"]],
    }


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
    parser.add_argument("--db-env-file", type=Path)
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
            else read_sheet(args.spreadsheet_token, args.sheet_id)
        )
        reader = SalesReader(WarehouseSettings.load(args.db_env_file))
        report = inspect_sales(reader, snapshot, config, args.as_of)
        write_report(args.output, report, snapshot)
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, subprocess.SubprocessError) as error:
        LOG.error("检查未完成：%s", error)
        return 1


def main() -> None:
    raise SystemExit(run())

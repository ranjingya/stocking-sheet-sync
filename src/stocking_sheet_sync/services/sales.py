from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from stocking_sheet_sync.domain.products import inspect_sheet, match_catalog, normalize_text
from stocking_sheet_sync.infrastructure.warehouse import SalesReader

LOG = logging.getLogger(__name__)


def resolve_sheet_styles(reader: SalesReader, snapshot: dict, config: dict) -> None:
    """
    功能说明：按唯一69码及名称规格确认不规范款号，保存本次内部款号映射。

    参数：
        reader：只读商品主数据读取器。
        snapshot：原始工作表快照，仅用于读取，不修改单元格。
        config：本次运行配置，resolved_styles保存按SKU识别的真实款号及原填写值。
    返回值：无；无法确认的商品行保留原款号，交由身份核验阻断。
    """
    rows = [
        row
        for row in inspect_sheet(snapshot, config)["rows"]
        if not re.fullmatch(r"kq\d+", normalize_text(row["style"]))
        and re.fullmatch(r"69\d{11}", row["sku"] or "")
        and not row["issues"]
    ]
    if not rows:
        return
    catalog = reader.catalog(config["catalog"], sorted({r["sku"] for r in rows}))
    by_sku = defaultdict(list)
    for record in catalog:
        by_sku[record["sku"]].append(record)
    for row in rows:
        candidates = by_sku[row["sku"]]
        if len(candidates) != 1:
            continue
        record = candidates[0]
        if not re.fullmatch(r"kq\d+", normalize_text(record["style"])):
            continue
        if any(
            not normalize_text(row[key]) or normalize_text(row[key]) != normalize_text(record[key])
            for key in ("name", "spec")
        ):
            continue
        config.setdefault("resolved_styles", {})[row["sku"]] = {
            "original": normalize_text(row["style"]),
            "style": record["style"],
        }
        LOG.info("第%s行：按SKU %s识别款号为%s", row["row"], row["sku"], record["style"])


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
        if config.get("selected_platforms") and platform["id"] not in config["selected_platforms"]:
            continue
        preserved = set()
        if config.get("incremental"):
            from stocking_sheet_sync.domain.sheets.incremental import fill_state

            source_rows = []
            for style in sorted({row["style"] for row in layout["rows"]}):
                scoped = [row for row in layout["rows"] if row["style"] == style]
                state = fill_state(
                    snapshot,
                    scoped,
                    layout["columns"][platform["id"]],
                    forecast=False,
                    overwrite=config.get("overwrite", False),
                )
                LOG.info("%s｜%s：%s", style, platform["name"], state)
                if state == "已完整跳过":
                    preserved.add(style)
                    continue
                try:
                    part = reader.sales(platform, sorted({r["sku"] for r in scoped}), as_of)
                except (RuntimeError, ValueError) as error:
                    part = {
                        "platform": platform["id"],
                        "rows": [],
                        "issues": ["source_read_failed"],
                        "error": str(error),
                    }
                sources.append({**part, "style": style})
                if part["issues"]:
                    source_rows.extend(
                        {
                            "sku": r["sku"],
                            "quantity": None,
                            "status": "needs_review",
                            "issues": part["issues"],
                        }
                        for r in scoped
                    )
                else:
                    source_rows.extend(part["rows"])
            source = {"rows": source_rows, "issues": []}
        else:
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
                "preserved": row["style"] in preserved,
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
        "catalog": catalog,
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

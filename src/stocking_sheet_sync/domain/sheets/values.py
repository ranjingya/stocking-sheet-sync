from __future__ import annotations

import logging
from collections import Counter, defaultdict

from stocking_sheet_sync.domain.products import column_number, inspect_sheet, units
from stocking_sheet_sync.domain.sheets.totals import column_total_rows, supplement_demand_totals

LOG = logging.getLogger(__name__)


def build_sales_update(
    snapshot: dict, report: dict, config: dict, *, partial: bool = False
) -> dict:
    """
    功能说明：核对完整销量候选清单，为空白销量单元格及对应平台合计生成请求。

    参数：
        snapshot：本次读取的完整表格值、公式与样式快照。
        report：基于同一快照和指定日期生成的数仓检查结果。
        config：商品字段、平台来源及表头别名配置。
        partial：是否允许跳过异常平台并填写其余平台。

    返回值：写入请求、逐项状态、平台合计和统计；存在任何异常时操作清单为空。
    """
    layout = inspect_sheet(snapshot, config)
    if layout["issues"] or not layout["rows"]:
        raise ValueError("商品或平台列结构不完整，请先核对表头")
    if report["spreadsheet_token"] != snapshot["spreadsheet_token"]:
        raise ValueError("销量检查结果与目标表格不一致")
    expected = {
        (p["id"], row["row"]): (row["sku"], f"{layout['columns'][p['id']]['sales']}{row['row']}")
        for p in config["platforms"]
        for row in layout["rows"]
    }
    actual = [(e["platform"], e["row"]) for e in report["entries"]]
    if len(actual) != len(expected) or set(actual) != expected.keys():
        raise ValueError("销量候选条数或商品平台组合不完整")
    entries, pending = [], defaultdict(list)
    totals = defaultdict(int)
    for entry in report["entries"]:
        sku, target = expected[(entry["platform"], entry["row"])]
        if entry["sku"] != sku or entry["target_cell"] != target:
            raise ValueError("销量候选的商品编码或目标单元格不一致")
        cell = snapshot["cells"][target]
        problems = set(entry["issues"]) - {"target_not_empty"}
        quantity = entry["observed_quantity"]
        if quantity is None:
            problems.add("quantity_missing")
        else:
            quantity = units(quantity)
        value = cell.get("value")
        equal = type(value) in (int, float) and value == quantity
        if cell.get("formula") or (value not in (None, "") and not equal):
            problems.add("target_conflict")
        status = "needs_review" if problems else "unchanged" if equal else "write"
        item = {**entry, "quantity": quantity, "status": status, "issues": sorted(problems)}
        entries.append(item)
        if status != "needs_review":
            totals[entry["platform"]] += quantity
        if status == "write":
            pending[target.rstrip("0123456789")].append(item)
        LOG.debug(
            "销量填充核对：platform=%s sku=%s cell=%s status=%s issues=%s",
            entry["platform"],
            sku,
            target,
            status,
            sorted(problems),
        )
    counts = Counter(e["status"] for e in entries)
    operations = []
    if not counts["needs_review"]:
        for col in sorted(pending, key=column_number):
            # 按连续商品行分块，空行、合计行及已有值不会进入写入范围。
            blocks = []
            for entry in sorted(pending[col], key=lambda e: e["row"]):
                if (
                    not blocks
                    or entry["row"] != blocks[-1][-1]["row"] + 1
                    or len(blocks[-1]) >= 100
                ):
                    blocks.append([])
                blocks[-1].append(entry)
            for block in blocks:
                operations.append(
                    {
                        "range": (
                            f"{snapshot['sheet_id']}!{col}{block[0]['row']}:{col}{block[-1]['row']}"
                        ),
                        "values": [[e["quantity"]] for e in block],
                    }
                )
    total_entries = []
    if partial or not counts["needs_review"]:
        for platform, columns in layout["columns"].items():
            for total in column_total_rows(
                snapshot,
                columns["demand"],
                columns["sales"],
                [row["row"] for row in layout["rows"]],
            ):
                cell = snapshot["cells"][total["target_cell"]]
                if cell.get("formula"):
                    status = "preserved"
                    total["formula"] = cell["formula"]
                elif cell.get("value") not in (None, ""):
                    status = "needs_review"
                else:
                    status = "write"
                    address = total["target_cell"]
                    operations.append(
                        {
                            "range": f"{snapshot['sheet_id']}!{address}:{address}",
                            "values": [[{"type": "formula", "text": total["formula"]}]],
                        }
                    )
                total_entries.append(
                    {
                        **total,
                        "platform": platform,
                        "status": status,
                        "expected_quantity": totals[platform],
                    }
                )
                LOG.debug(
                    "平台合计核对：platform=%s cell=%s status=%s",
                    platform,
                    total["target_cell"],
                    status,
                )
    total_conflicts = sum(e["status"] == "needs_review" for e in total_entries)
    if total_conflicts:
        operations = []
    summary = {
        "expected_cells": len(expected),
        "write": counts["write"],
        "unchanged": counts["unchanged"],
        "needs_review": counts["needs_review"] + total_conflicts,
        "total_formulas_to_write": sum(e["status"] == "write" for e in total_entries),
        "platform_totals": dict(totals) if not counts["needs_review"] else None,
    }
    LOG.debug("销量填充请求生成：operations=%d summary=%s", len(operations), summary)
    result = {
        "as_of": report["as_of"],
        "entries": entries,
        "total_entries": total_entries,
        "operations": operations,
        "summary": summary,
        "status": "needs_review"
        if counts["needs_review"] or total_conflicts
        else "changes_proposed"
        if operations
        else "unchanged",
    }

    if partial:
        from stocking_sheet_sync.domain.sheets.platforms import isolate_platforms

        result = isolate_platforms(result, snapshot["sheet_id"])
    supplement_demand_totals(
        result,
        snapshot,
        {
            pid: cols["demand"]
            for pid, cols in layout["columns"].items()
            if pid not in result.get("blocked_platforms", {})
        },
        [r["row"] for r in layout["rows"]],
    )
    return result

from __future__ import annotations

import logging
from collections import Counter, defaultdict

from stocking_sheet_sync.domain.products import column_number, inspect_sheet, units
from stocking_sheet_sync.domain.sheets.comparison import comparable_layout, log_height_changes
from stocking_sheet_sync.domain.sheets.totals import column_total_rows

LOG = logging.getLogger(__name__)


def build_sales_update(snapshot: dict, report: dict, config: dict) -> dict:
    """
    功能说明：核对完整销量候选清单，为空白销量单元格及对应平台合计生成请求。

    参数：
        snapshot：本次读取的完整表格值、公式与样式快照。
        report：基于同一快照和指定日期生成的数仓检查结果。
        config：商品字段、平台来源及表头别名配置。

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
        LOG.info(
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
    if not counts["needs_review"]:
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
                LOG.info(
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
    LOG.info("销量填充请求生成：operations=%d summary=%s", len(operations), summary)
    return {
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


def verify_sales_update(before: dict, after: dict, update: dict) -> dict:
    """
    功能说明：全量回读核对销量、平台合计及非目标内容，记录公式自动重算。

    参数：
        before：写入前完整快照。
        after：写入后完整快照。
        update：本次核对通过的销量填充请求与逐项结果。

    返回值：校验统计与公式重算记录；内容、样式或结构异常时抛出错误。
    """
    for key in ("spreadsheet_token", "sheet_id", "row_count", "column_count", "merges"):
        if before[key] != after[key]:
            raise ValueError(f"写入后表格身份或结构发生变化：{key}")
    if before["cells"].keys() != after["cells"].keys():
        raise ValueError("回读单元格范围不完整")
    log_height_changes(before["layout"], after["layout"])
    old_layout = comparable_layout(before["layout"])
    new_layout = comparable_layout(after["layout"])
    for key in old_layout.keys() | new_layout.keys():
        if key != "revision" and old_layout.get(key) != new_layout.get(key):
            raise ValueError(f"写入后布局发生变化：{key}")
    targets = {e["target_cell"]: e for e in update["entries"]}
    total_cells = {e["target_cell"]: e for e in update.get("total_entries", [])}
    totals, recalculated = defaultdict(int), []
    for address, old in before["cells"].items():
        new = after["cells"][address]
        old_rest, new_rest = dict(old), dict(new)
        if address in targets:
            entry = targets[address]
            value = new.get("value")
            if type(value) not in (int, float) or value != entry["quantity"] or new.get("formula"):
                raise ValueError(f"销量回读数量或类型不一致：{address}")
            totals[entry["platform"]] += value
            old_rest.pop("value", None)
            new_rest.pop("value", None)
        elif address in total_cells:
            total = total_cells[address]
            if (
                new.get("formula") != total["formula"]
                or new.get("value") != total["expected_quantity"]
            ):
                raise ValueError(f"平台合计公式或计算值不符合预期：{address}")
            if type(new.get("value")) not in (int, float):
                raise ValueError(f"平台合计没有返回数值：{address}")
            for key in ("formula", "value"):
                old_rest.pop(key, None)
                new_rest.pop(key, None)
        elif old.get("formula"):
            # 仅允许原公式计算结果随输入更新，公式文本及格式必须保留。
            old_value, new_value = old_rest.pop("value", None), new_rest.pop("value", None)
            if old_value != new_value:
                if isinstance(new_value, str) and new_value.startswith("#"):
                    raise ValueError(f"公式重算出现错误：{address} {new_value}")
                recalculated.append(
                    {
                        "cell": address,
                        "formula": old["formula"],
                        "before": old_value,
                        "after": new_value,
                    }
                )
        if old_rest != new_rest:
            raise ValueError(f"单元格内容、公式或格式发生意外变化：{address}")
    if dict(totals) != update["summary"]["platform_totals"]:
        raise ValueError("回读平台销量合计不一致")
    return {
        "verified": True,
        "as_of": update["as_of"],
        "checked_sales_cells": len(targets),
        "checked_total_formulas": len(total_cells),
        "checked_all_cells": len(before["cells"]),
        "platform_totals": dict(totals),
        "recalculated_formulas": recalculated,
        "before_revision": before["revision"],
        "after_revision": after["revision"],
    }

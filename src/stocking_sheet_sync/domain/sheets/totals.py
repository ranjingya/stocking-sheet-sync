from __future__ import annotations

import logging
import re

from stocking_sheet_sync.domain.products import column_name, column_number, normalize_text

LOG = logging.getLogger(__name__)

SUM_RANGE = re.compile(r"=SUM\(\$?([A-Z]+)\$?(\d+):\$?\1\$?(\d+)\)", re.I)


def supplement_row_summaries(update: dict, snapshot: dict, config: dict, rows: list[int]) -> None:
    """
    功能说明：将商品行需求汇总限定为配置匹配的人工需求列，保留销量和预估数据。

    参数：
        update：待补充的写入计划，原地追加公式与核验信息。
        snapshot：包含表头、合并区域及单元格值的完整快照。
        config：需求汇总标题、人工需求标题及平台别名配置。
        rows：需要汇总的商品行号。
    返回值：无；汇总列歧义或来源错误时拒绝生成计划。
    """
    matching = config["matching"]
    aliases = {normalize_text(v) for v in matching.get("summary_headers", [])}
    if not aliases or update["summary"]["needs_review"] or not rows:
        return
    header_rows = matching["header_rows"]
    headers = {a: c.get("value") for a, c in snapshot["cells"].items()}
    for area in snapshot.get("merges", []):
        left, top, right, bottom = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", area).groups()
        for row in range(int(top), min(int(bottom), header_rows) + 1):
            for col in range(column_number(left), column_number(right) + 1):
                headers[f"{column_name(col)}{row}"] = headers.get(f"{left}{top}")
    labels = {}
    for index in range(1, snapshot["column_count"] + 1):
        col = column_name(index)
        # 只匹配最底层标题，避免将平台分组下的销量或预估列当成人工需求。
        values = [normalize_text(headers.get(f"{col}{r}")) for r in range(1, header_rows + 1)]
        labels[col] = next((v for v in reversed(values) if v), "")
    summaries = [col for col, label in labels.items() if label in aliases]
    if not summaries:
        return
    if len(summaries) != 1:
        raise ValueError("需求汇总列不唯一，请核对表头")
    target_col = summaries[0]
    demands = {normalize_text(v) for v in matching.get("other_demand_headers", [])}
    demands.update(normalize_text(v) for p in config["platforms"] for v in p["demand_headers"])
    columns = [col for col, label in labels.items() if label in demands]
    if not columns or any(column_number(c) >= column_number(target_col) for c in columns):
        raise ValueError("需求汇总的人工需求列缺失或位置异常，请核对表头")
    count = 0
    for row in rows:
        address = f"{target_col}{row}"
        formula = "=SUM(" + ",".join(f"{col}{row}" for col in columns) + ")"
        cell = snapshot["cells"].get(address, {})
        if cell.get("formula") == formula:
            continue
        values = [snapshot["cells"].get(f"{col}{row}", {}).get("value") for col in columns]
        if any(isinstance(v, str) and v.startswith("#") for v in values):
            raise ValueError(f"需求汇总来源存在错误值：{address}")
        update.setdefault("summary_replacements", {})[address] = cell.get("formula") or cell.get(
            "value"
        )
        update["total_entries"].append(
            {
                "target_cell": address,
                "formula": formula,
                "platform": "demand_summary",
                "source_cells": [f"{col}{row}" for col in columns],
                "status": "write",
                "expected_quantity": sum(v for v in values if type(v) in (int, float)),
            }
        )
        update["operations"].append(
            {
                "range": f"{snapshot['sheet_id']}!{address}:{address}",
                "values": [[{"type": "formula", "text": formula}]],
            }
        )
        count += 1
    if count:
        update["status"] = "changes_proposed"
        update["summary"]["total_formulas_to_write"] = (
            update["summary"].get("total_formulas_to_write", 0) + count
        )
        LOG.info(
            "需求汇总核对完成：sheet_id=%s demand_columns=%s formulas=%d",
            snapshot["sheet_id"],
            columns,
            count,
        )


def column_total_rows(
    snapshot: dict, demand_column: str, sales_column: str, product_rows: list[int]
) -> list[dict]:
    """
    功能说明：根据需求列或唯一的同范围合计行，生成目标列合计公式。

    参数：
        snapshot：含完整单元格和行数的工作表快照。
        demand_column：现有平台需求列，用于定位原表合计。
        sales_column：对应的销量列。
        product_rows：本表全部商品行号，允许商品之间有空行。

    返回值：合计单元格、原公式位置及目标公式；无法明确识别时返回空列表。
    """
    if not product_rows:
        return []
    first, last = min(product_rows), max(product_rows)
    results = []
    for row in range(last + 1, snapshot["row_count"] + 1):
        source = f"{demand_column}{row}"
        formula = snapshot["cells"][source].get("formula", "")
        match = SUM_RANGE.fullmatch(re.sub(r"\s+", "", formula))
        if not match or match.group(1).upper() != demand_column:
            continue
        if (int(match.group(2)), int(match.group(3))) != (first, last):
            continue
        results.append(
            {
                "target_cell": f"{sales_column}{row}",
                "source_cell": source,
                "formula": f"=SUM({sales_column}{first}:{sales_column}{last})",
            }
        )
    if results:
        return results
    # 同一合计行的其他列必须完整覆盖全部商品，且只能定位到一行。
    peers = {}
    for address, cell in snapshot["cells"].items():
        match = SUM_RANGE.fullmatch(re.sub(r"\s+", "", cell.get("formula", "")))
        position = re.fullmatch(r"([A-Z]+)(\d+)", address)
        if not match or not position:
            continue
        row = int(position.group(2))
        if row <= last or match.group(1).upper() != position.group(1):
            continue
        if (int(match.group(2)), int(match.group(3))) == (first, last):
            peers[row] = address
    if len(peers) != 1:
        return []
    row, source = next(iter(peers.items()))
    demand = snapshot["cells"].get(f"{demand_column}{row}", {})
    if demand.get("formula") or demand.get("value") not in (None, ""):
        return []
    return [
        {
            "target_cell": f"{sales_column}{row}",
            "source_cell": source,
            "formula": f"=SUM({sales_column}{first}:{sales_column}{last})",
        }
    ]


def supplement_demand_totals(update: dict, snapshot: dict, columns: dict, rows: list[int]) -> None:
    """根据snapshot和商品rows定位合计，为columns中的空白需求合计补公式；修改update，无返回值。"""
    if update["summary"]["needs_review"] or not rows:
        return
    for pid, col in columns.items():
        for total in column_total_rows(snapshot, col, col, rows):
            cell = snapshot["cells"].get(total["target_cell"], {})
            if cell.get("formula") or cell.get("value") not in (None, ""):
                continue
            values = [
                snapshot["cells"].get(f"{col}{row}", {}).get("value")
                for row in range(min(rows), max(rows) + 1)
            ]
            if any(isinstance(v, str) and v.startswith("#") for v in values):
                continue
            quantity = sum(v for v in values if type(v) in (int, float))
            update["total_entries"].append(
                {**total, "platform": pid, "status": "write", "expected_quantity": quantity}
            )
            address = total["target_cell"]
            update["operations"].append(
                {
                    "range": f"{snapshot['sheet_id']}!{address}:{address}",
                    "values": [[{"type": "formula", "text": total["formula"]}]],
                }
            )
            if update["status"] == "unchanged":
                update["status"] = "changes_proposed"
            update["summary"]["total_formulas_to_write"] = (
                update["summary"].get("total_formulas_to_write", 0) + 1
            )

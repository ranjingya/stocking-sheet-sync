from __future__ import annotations

import re

SUM_RANGE = re.compile(r"=SUM\(\$?([A-Z]+)\$?(\d+):\$?\1\$?(\d+)\)", re.I)


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

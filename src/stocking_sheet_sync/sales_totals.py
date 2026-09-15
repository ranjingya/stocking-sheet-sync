from __future__ import annotations

import re

SUM_RANGE = re.compile(r"=SUM\(\$?([A-Z]+)\$?(\d+):\$?\1\$?(\d+)\)", re.I)


def column_total_rows(
    snapshot: dict, demand_column: str, sales_column: str, product_rows: list[int]
) -> list[dict]:
    """
    功能说明：识别需求列覆盖全部商品的单列 SUM，生成同范围的销量合计公式。

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
    return results

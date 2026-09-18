from __future__ import annotations

import logging
import re

from .sales_reader import units
from .sheet_matching import column_name, normalize_text

LOG = logging.getLogger(__name__)


def read_company_sales(snapshot: dict | None, rows: list[dict], rules: dict) -> dict:
    """
    功能说明：按配置识别表内全公司去年生命周期销量列，保留逐SKU数值及来源证据。

    参数：
        snapshot：补列前的完整需求表快照；空值表示未提供表格。
        rows：已定位的表内商品行，包含SKU和行号。
        rules：包含公司销量表头匹配规则的预测配置。
    返回值：列位置、周期原文、逐SKU销量和异常；不查询数仓或修改原单元格。
    """
    result = {"status": "missing", "rows": [], "candidates": []}
    if snapshot is None:
        return result
    config = rules["company_sheet"]
    candidates = []
    for number in range(1, snapshot["column_count"] + 1):
        col = column_name(number)
        group = snapshot["cells"].get(f"{col}{config['group_row']}", {}).get("value", "")
        header = snapshot["cells"].get(f"{col}{config['header_row']}", {}).get("value", "")
        if re.fullmatch(
            config["group_pattern"], normalize_text(str(group or ""))
        ) or normalize_text(str(header or "")) in {normalize_text(v) for v in config["headers"]}:
            candidates.append({"column": col, "group": group, "period": header})
    result["candidates"] = candidates
    if len(candidates) != 1:
        result["status"] = "ambiguous" if candidates else "missing"
        LOG.info("表内全公司销量列识别：status=%s candidates=%d", result["status"], len(candidates))
        return result
    result.update(candidates[0], status="available")
    for row in rows:
        address = f"{result['column']}{row['row']}"
        raw = snapshot["cells"].get(address, {}).get("value")
        quantity, issue = None, None
        try:
            if isinstance(raw, bool):
                raise ValueError("布尔值不是销量")
            quantity = units(raw)
        except ValueError:
            issue = "company_quantity_missing_or_invalid"
        result["rows"].append(
            {"sku": row["sku"], "cell": address, "quantity": quantity, "issue": issue}
        )
    LOG.info("表内全公司生命周期销量读取：column=%s rows=%d", result["column"], len(rows))
    return result

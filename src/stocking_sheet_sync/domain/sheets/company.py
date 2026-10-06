from __future__ import annotations

import logging
from datetime import timedelta

from stocking_sheet_sync.domain.products import column_name, normalize_text, units

LOG = logging.getLogger(__name__)


def read_company_sales(snapshot: dict | None, rows: list[dict], rules: dict, *, as_of=None) -> dict:
    """
    功能说明：识别带日期的全公司近30天销量列，核对周期并保留逐SKU来源证据。

    参数：
        snapshot：补列前的完整需求表快照；空值表示未提供表格。
        rows：已定位的表内商品行，包含SKU和行号。
        rules：包含公司销量表头匹配规则的预测配置。
        as_of：预测基准日；未提供时不将表内数量用于预测。
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
        generated = normalize_text(str(header or "")).startswith(
            normalize_text(config.get("generated_prefix", "全公司近30天出库 "))
        )
        if generated:
            candidates.append(
                {"column": col, "group": group, "period": header, "generated": generated}
            )
    result["candidates"] = candidates
    selected = candidates
    if len(selected) != 1:
        result["status"] = "ambiguous" if candidates else "missing"
        LOG.debug(
            "表内全公司销量列识别：status=%s candidates=%d", result["status"], len(candidates)
        )
        return result
    result.update(selected[0], status="available")
    if as_of is None:
        result.update(status="unavailable", reasons=["全公司：缺少近30天基准日"])
        return result
    start, end = as_of - timedelta(days=30), as_of - timedelta(days=1)
    expected = config["generated_prefix"] + (
        f"{start:%y}.{start.month}.{start.day}-{end:%y}.{end.month}.{end.day}"
    )
    if normalize_text(str(result["period"])) != normalize_text(expected):
        result.update(status="unavailable", reasons=["全公司：表内近30天日期与基准日不一致"])
        return result
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
    LOG.debug("表内全公司近30天销量读取：column=%s rows=%d", result["column"], len(rows))
    return result


def append_company_values(snapshot, update, source, config):
    """
    功能说明：把已核实的全公司出库加入数量写入计划，异常SKU独立跳过。

    参数：
        snapshot：补列后的工作表快照。
        update：已有平台数量计划。
        source：公司销量解析或自动查询结果。
        config：商品表头及强制覆盖配置。
    返回值：包含全公司数量的计划；不覆盖公式或未获准替换的数值。
    """
    if not source or not source.get("automatic") or update.get("target_issues"):
        return update
    if update["summary"].get("needs_review") and not (
        update.get("blocked_platforms") or update.get("blocked_forecasts")
    ):
        return update
    initial_count = len(update["entries"])
    from stocking_sheet_sync.domain.products import inspect_sheet

    header_row = config["matching"]["header_rows"]
    columns = [
        column_name(n)
        for n in range(1, snapshot["column_count"] + 1)
        if snapshot["cells"].get(f"{column_name(n)}{header_row}", {}).get("value")
        == source["header"]
    ]
    if len(columns) != 1:
        raise ValueError("全公司出库目标列不唯一")
    col = columns[0]
    identities = {r["row"]: r["sku"] for r in inspect_sheet(snapshot, config)["rows"]}
    for item in source["rows"]:
        if identities.get(item["row"]) != item["sku"]:
            raise ValueError("全公司出库商品行身份发生变化")
        if item["quantity"] is None:
            continue
        address = f"{col}{item['row']}"
        cell = snapshot["cells"][address]
        value = cell.get("value")
        equal = type(value) in (int, float) and value == item["quantity"]
        if cell.get("formula") or (
            value not in (None, "") and not equal and not config.get("overwrite")
        ):
            raise ValueError(f"全公司出库目标单元格发生变化：{address}")
        entry = {
            **item,
            "target_cell": address,
            "platform": "company:sales",
            "metric": "sales",
            "status": "unchanged" if equal else "write",
        }
        update["entries"].append(entry)
        if not equal:
            update["operations"].append(
                {
                    "range": f"{snapshot['sheet_id']}!{address}:{address}",
                    "values": [[item["quantity"]]],
                }
            )
        summary = update["summary"]
        summary[entry["status"]] = summary.get(entry["status"], 0) + 1
        totals = summary["platform_totals"]
        totals["company:sales"] = totals.get("company:sales", 0) + item["quantity"]
    added = len(update["entries"]) - initial_count
    if added:
        update["summary"]["needs_review"] = 0
        if "expected_cells" in update["summary"]:
            update["summary"]["expected_cells"] += added
        if update["status"] == "needs_review":
            update["status"] = "partial"
    return update

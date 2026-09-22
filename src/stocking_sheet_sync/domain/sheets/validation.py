from __future__ import annotations

import logging
from collections import defaultdict

from stocking_sheet_sync.domain.products import column_name, column_number
from stocking_sheet_sync.domain.sheets.comparison import comparable_layout, log_height_changes
from stocking_sheet_sync.domain.sheets.layout import _bounds, plan_market_layout


def verify_update(before: dict, after: dict, update: dict, config: dict, rules: dict) -> dict:
    """
    功能说明：核对新增字段、原内容与样式、行高和合并范围，并验证再次运行无操作。

    参数：
        before：写入前完整快照。
        after：写入后完整快照。
        update：本次顺序执行请求及列映射。
        config：商品与平台别名配置。
        rules：市场部结构规则。

    返回值：核验统计；任一检查失败抛出异常，不尝试自动重写。
    """
    if after["row_count"] != before["row_count"] or after["column_count"] != before[
        "column_count"
    ] + len(update["inserted_columns"]):
        raise ValueError("插列后的行列数量不符合预期")
    for key in ("spreadsheet_token", "sheet_id", "title"):
        if before.get(key) != after.get(key):
            raise ValueError(f"回读表格身份变化：{key}")
    log_height_changes(before["layout"], after["layout"])
    old_layout = comparable_layout(before["layout"])
    new_layout = comparable_layout(after["layout"])
    for key in ("hidden", "sheet_format", "data_validations", "row_dimensions"):
        if old_layout.get(key) != new_layout.get(key):
            raise ValueError(f"原工作表布局发生变化：{key}")
    if "column_dimensions" in before["layout"]:
        for old, new in update["column_mapping"].items():
            if _column_dimension(before, old) != _column_dimension(after, new):
                raise ValueError(f"原列宽、隐藏状态或列样式发生变化：{old}")
    plan = plan_market_layout(
        after, config, rules, recent_only=True, forecast=update.get("forecast", False)
    )
    if plan["status"] != "ready" or plan["operations"]:
        raise ValueError("回读后的市场部结构不符合规则")
    group_row = rules["layout"]["group_row"]
    header_row = config["matching"]["header_rows"]
    original_market = {f["column"] for f in update["report"]["existing_fields"]}
    renamed = {
        f["source_column"]
        for f in update["report"]["target_fields"]
        if f["source_column"] and f["metric"] in {"sales", "forecast", "future"}
    }
    formulas = []
    checked = 0
    for old_col, new_col in update["column_mapping"].items():
        for row in range(1, before["row_count"] + 1):
            old, new = before["cells"][f"{old_col}{row}"], after["cells"][f"{new_col}{row}"]
            if (row == group_row and old_col in original_market) or (
                row == header_row and old_col in renamed
            ):
                continue
            # 插列由飞书调整公式引用；核对公式保留及计算值，记录原式与新式供审阅。
            if old.get("formula"):
                if not new.get("formula") or old.get("value") != new.get("value"):
                    raise ValueError(f"原公式或结果发生异常变化：{old_col}{row}")
                if old["formula"] != new["formula"]:
                    formulas.append(
                        {
                            "before_cell": f"{old_col}{row}",
                            "after_cell": f"{new_col}{row}",
                            "before": old["formula"],
                            "after": new["formula"],
                        }
                    )
            elif old.get("value") != new.get("value") or new.get("formula"):
                raise ValueError(f"原单元格内容发生变化：{old_col}{row}")
            for key in ("cell_styles", "border_styles", "note", "data_validation", "rich_text"):
                if old.get(key) != new.get(key):
                    raise ValueError(f"原单元格格式或备注发生变化：{old_col}{row} {key}")
            checked += 1
    for field in update["report"]["target_fields"]:
        if field["source_column"]:
            continue
        target = field["target_column"]
        demand = next(
            f["target_column"]
            for f in update["report"]["target_fields"]
            if (field["platform"] == "company" or f["platform"] == field["platform"])
            and f["metric"] == "demand"
        )
        for row in range(header_row, after["row_count"] + 1):
            cell = after["cells"][f"{target}{row}"]
            total_formula = update.get("total_formulas", {}).get(f"{target}{row}")
            if total_formula:
                if cell.get("formula") != total_formula or cell.get("value") != 0:
                    raise ValueError(f"新增销量列合计公式或结果不符合预期：{target}{row}")
            elif row > header_row and (cell.get("value") not in (None, "") or cell.get("formula")):
                raise ValueError(f"新增销量列商品行不应带入数量或公式：{target}{row}")
            for key in ("cell_styles", "border_styles"):
                if cell.get(key) != after["cells"][
                    f"{update.get('inherited_columns', {}).get(target, demand)}{row}"
                ].get(key):
                    raise ValueError(f"新增销量列未继承需求列样式：{target}{row}")
    expected_merges = set()
    changed_groups = {
        op["before_range"]
        for op in update["report"]["operations"]
        if op["action"] == "set_market_group_header"
    }
    for area in before["merges"]:
        if area in changed_groups:
            continue
        left, top, right, bottom = _bounds(area)
        expected_merges.add(
            f"{update['column_mapping'][column_name(left)]}{top}:{update['column_mapping'][column_name(right)]}{bottom}"
        )
    expected_merges.update(
        op["after_range"]
        for op in update["report"]["operations"]
        if op["action"] == "set_market_group_header"
    )
    if expected_merges != set(after["merges"]):
        raise ValueError("合并范围不符合插列映射")
    return {
        "verified": True,
        "before_revision": before["revision"],
        "after_revision": after["revision"],
        "checked_original_cells": checked,
        "inserted_columns": update["inserted_columns"],
        "formula_reference_changes": formulas,
        "repeat_operation_count": 0,
    }


def _column_dimension(snapshot: dict, col: str) -> dict:
    """提取 snapshot 中 col 的列尺寸及格式，排除随插列变化的位置属性。"""
    number = column_number(col)
    for dimension in snapshot["layout"]["column_dimensions"].values():
        if int(dimension["min"]) <= number <= int(dimension["max"]):
            return {k: v for k, v in dimension.items() if k not in {"min", "max"}}
    return {}


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


LOG = logging.getLogger(__name__)

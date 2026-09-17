from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from copy import deepcopy

from .sales_reader import units
from .sales_totals import column_total_rows
from .sheet_layout import plan_market_layout
from .sheet_matching import column_name, column_number, inspect_sheet, match_catalog

LOG = logging.getLogger(__name__)
INPUT_METRICS = {"sales": "current", "previous": "previous", "future": "historical_future"}


def check_forecast_target(snapshot: dict, report: dict, config: dict) -> dict:
    """
    功能说明：核对试算款式、目标商品身份与整款SKU集合，防止部分SKU承接整款需求。

    参数：
        snapshot：目标表完整快照。
        report：整款数仓试算报告，含商品主数据及各平台结果。
        config：商品字段和平台配置。
    返回值：商品行及缺失、多余、身份异常；存在问题时禁止写入。
    """
    layout = inspect_sheet(snapshot, config)
    groups = report["groups"]
    catalog = [record for group in groups for record in group["catalog"]]
    match_catalog(layout, catalog)
    issues = []
    by_style = defaultdict(set)
    for row in layout["rows"]:
        by_style[row["style"]].add(row["sku"])
        if row["issues"]:
            issues.append(
                {
                    "reason": "product_identity_unresolved",
                    "row": row["row"],
                    "sku": row["sku"],
                    "details": row["issues"],
                }
            )
    if not layout["rows"]:
        issues.append({"reason": "no_product_rows"})
    expected_styles = {group["style"] for group in groups}
    if len(expected_styles) != len(groups) or expected_styles != set(by_style):
        issues.append({"reason": "style_set_mismatch"})
    for group in groups:
        expected = set(group["skus"])
        actual = by_style.get(group["style"], set())
        if expected != actual:
            issues.append(
                {
                    "reason": "incomplete_style_skus",
                    "style": group["style"],
                    "missing_skus": sorted(expected - actual),
                    "extra_skus": sorted(actual - expected, key=str),
                }
            )
        if group["issues"]:
            issues.append(
                {
                    "reason": "catalog_or_season_unresolved",
                    "style": group["style"],
                    "details": group["issues"],
                }
            )
    LOG.info("预测目标核对完成：rows=%d issues=%d", len(layout["rows"]), len(issues))
    return {"rows": layout["rows"], "issues": issues}


def project_layout(snapshot: dict, update: dict, config: dict, rules: dict) -> dict:
    """
    功能说明：在内存中映射补列后的单元格坐标，用于写前发现已有值冲突。

    参数：
        snapshot：原始完整快照。
        update：已经校验的插列计划及原列映射。
        config：字段匹配配置。
        rules：市场部分组行规则。
    返回值：仅用于填充值预检的投影快照；真实写后必须读取服务端快照再次核验。
    """
    projected = deepcopy(snapshot)
    projected["column_count"] += len(update["inserted_columns"])
    projected["cells"] = {
        f"{column_name(c)}{r}": {}
        for c in range(1, projected["column_count"] + 1)
        for r in range(1, projected["row_count"] + 1)
    }
    for old, new in update["column_mapping"].items():
        for row in range(1, snapshot["row_count"] + 1):
            projected["cells"][f"{new}{row}"] = deepcopy(snapshot["cells"][f"{old}{row}"])
    group_row = rules["layout"]["group_row"]
    fields = update["report"]["target_fields"]
    for field in fields:
        col = field["target_column"]
        projected["cells"][f"{col}{config['matching']['header_rows']}"] = {"value": field["header"]}
        projected["cells"][f"{col}{group_row}"] = {}
    projected["cells"][f"{fields[0]['target_column']}{group_row}"] = {
        "value": rules["layout"]["market_header"]
    }
    # 投影只用于坐标与已有内容检查；真实公式引用和样式由服务端插列处理。
    from .sheet_layout import _bounds

    projected["merges"] = []
    for area in snapshot["merges"]:
        left, top, right, bottom = _bounds(area)
        if top == group_row == bottom and any(
            op.get("before_range") == area for op in update["report"]["operations"]
        ):
            continue
        projected["merges"].append(
            f"{update['column_mapping'][column_name(left)]}{top}:"
            f"{update['column_mapping'][column_name(right)]}{bottom}"
        )
    for op in update["report"]["operations"]:
        if op["action"] == "set_market_group_header":
            projected["merges"].append(op["after_range"])
    return projected


def build_forecast_values(
    snapshot: dict,
    report: dict,
    config: dict,
    rules: dict,
    *,
    history: bool,
    forecast: bool,
) -> dict:
    """
    功能说明：为已补齐结构的老款表生成历史与公式预估数量请求，并保护已有内容。

    参数：
        snapshot：已补列的实际快照或用于预检的内存投影。
        report：同一预估日的完整数仓试算证据。
        config：商品及平台匹配配置。
        rules：表头规则，包含公式预估字段。
        history：是否写入当前30天、去年同期30天和去年后续周期。
        forecast：是否写入公式计算所得的预估数量，人工需求始终保留。
    返回值：与通用销量回读核验器兼容的请求；任何目标或数据异常均返回空操作。
    """
    if not history and not forecast:
        raise ValueError("至少启用一项填充")
    checked = check_forecast_target(snapshot, report, config)
    plan = plan_market_layout(snapshot, config, rules, recent_only=True, forecast=True)
    if plan["status"] != "ready":
        raise ValueError("公式预估表头需要先补齐并回读核验")
    fields = {(f["platform"], f["metric"]): f["target_column"] for f in plan["target_fields"]}
    groups = {g["style"]: g for g in report["groups"]}
    issues = list(checked["issues"])
    entries, operations, totals = [], [], defaultdict(int)
    metrics = [*INPUT_METRICS] if history else []
    if forecast:
        metrics.append("forecast")
    expected_platforms = {p["id"] for p in config["platforms"]}
    for group in report["groups"]:
        actual = [p["platform"] for p in group["platforms"]]
        if len(actual) != len(expected_platforms) or set(actual) != expected_platforms:
            issues.append({"reason": "platform_results_incomplete", "style": group["style"]})
    for row in checked["rows"]:
        group = groups.get(row["style"])
        if not group:
            continue
        for platform in group["platforms"]:
            pid = platform["platform"]
            if pid not in expected_platforms:
                continue
            for metric in metrics:
                col = fields[(pid, metric)]
                address = f"{col}{row['row']}"
                value = (
                    platform.get("forecast", {}).get("rows", {}).get(row["sku"], {}).get("quantity")
                    if metric == "forecast" and platform["status"] == "ready"
                    else platform.get("inputs", {})
                    .get(INPUT_METRICS.get(metric), {})
                    .get(row["sku"])
                    if metric != "forecast"
                    else None
                )
                problems = []
                if value is None:
                    problems.append("quantity_unavailable")
                else:
                    value = units(value)
                cell = snapshot["cells"][address]
                existing = cell.get("value")
                equal = type(existing) in (int, float) and existing == value
                if cell.get("formula") or (existing not in (None, "") and not equal):
                    problems.append("target_conflict")
                status = "needs_review" if problems else "unchanged" if equal else "write"
                key = f"{pid}:{metric}"
                entry = {
                    "platform": key,
                    "metric": metric,
                    "style": row["style"],
                    "sku": row["sku"],
                    "row": row["row"],
                    "target_cell": address,
                    "quantity": value,
                    "status": status,
                    "issues": problems,
                }
                entries.append(entry)
                if not problems:
                    totals[key] += value
                if status == "write":
                    operations.append(
                        {
                            "range": f"{snapshot['sheet_id']}!{address}:{address}",
                            "values": [[value]],
                        }
                    )
    total_entries = []
    for pid in sorted(expected_platforms):
        for metric in metrics:
            key = f"{pid}:{metric}"
            for total in column_total_rows(
                snapshot,
                fields[(pid, "demand")],
                fields[(pid, metric)],
                [r["row"] for r in checked["rows"]],
            ):
                cell = snapshot["cells"][total["target_cell"]]
                status = (
                    "preserved"
                    if cell.get("formula")
                    else ("needs_review" if cell.get("value") not in (None, "") else "write")
                )
                if cell.get("formula"):
                    total["formula"] = cell["formula"]
                if status == "write":
                    address = total["target_cell"]
                    operations.append(
                        {
                            "range": f"{snapshot['sheet_id']}!{address}:{address}",
                            "values": [[{"type": "formula", "text": total["formula"]}]],
                        }
                    )
                total_entries.append(
                    {**total, "platform": key, "status": status, "expected_quantity": totals[key]}
                )
    counts = Counter(e["status"] for e in entries)
    problems = (
        len(issues)
        + counts["needs_review"]
        + sum(e["status"] == "needs_review" for e in total_entries)
    )
    LOG.info("公式预估填充请求生成：entries=%d issues=%d", len(entries), problems)
    return {
        "as_of": report["as_of"],
        "entries": entries,
        "total_entries": total_entries,
        "operations": [] if problems else compact_ranges(operations),
        "target_issues": issues,
        "summary": {
            "expected_cells": len(entries),
            "needs_review": problems,
            "write": counts["write"],
            "unchanged": counts["unchanged"],
            "platform_totals": dict(totals),
        },
        "status": "needs_review" if problems else "changes_proposed" if operations else "unchanged",
    }


def compact_ranges(operations: list[dict]) -> list[dict]:
    """把 operations 中连续同列单格请求合并为最多100行的块，返回批量范围。"""
    cells = []
    for operation in operations:
        sid, area = operation["range"].split("!", 1)
        match = re.fullmatch(r"([A-Z]+)([0-9]+):\1\2", area)
        if not match:
            raise ValueError("合并请求只接受单格范围")
        col, row = match.groups()
        cells.append((sid, col, int(row), operation["values"][0]))
    cells.sort(key=lambda cell: (cell[0], column_number(cell[1]), cell[2]))
    blocks = []
    for sid, col, row, value in cells:
        if (
            blocks
            and blocks[-1]["sid"] == sid
            and blocks[-1]["col"] == col
            and blocks[-1]["last"] + 1 == row
            and len(blocks[-1]["values"]) < 100
        ):
            blocks[-1]["last"] = row
            blocks[-1]["values"].append(value)
        else:
            blocks.append({"sid": sid, "col": col, "first": row, "last": row, "values": [value]})
    return [
        {"range": f"{b['sid']}!{b['col']}{b['first']}:{b['col']}{b['last']}", "values": b["values"]}
        for b in blocks
    ]

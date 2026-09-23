from __future__ import annotations

import logging
from collections import Counter, defaultdict
from copy import deepcopy

from stocking_sheet_sync.domain.products import (
    column_name,
    inspect_sheet,
    match_catalog,
    units,
)
from stocking_sheet_sync.domain.sheets.layout import plan_market_layout
from stocking_sheet_sync.domain.sheets.platforms import compact_ranges
from stocking_sheet_sync.domain.sheets.totals import column_total_rows, supplement_demand_totals

LOG = logging.getLogger(__name__)
INPUT_METRICS = {"sales": "current", "previous": "previous", "future": "historical_future"}


def check_forecast_target(snapshot: dict, report: dict, config: dict) -> dict:
    """
    功能说明：核对需求表各行的商品身份及计算数据是否可用。

    参数：
        snapshot：目标表完整快照。
        report：需求表SKU的数仓试算报告，含商品主数据及各平台结果。
        config：商品字段和平台配置。
    返回值：商品行及未匹配、身份异常；存在问题时禁止写入。
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
        if group["issues"]:
            issues.append(
                {
                    "reason": "catalog_or_season_unresolved",
                    "style": group["style"],
                    "details": group["issues"],
                }
            )
    LOG.debug("预测目标核对完成：rows=%d issues=%d", len(layout["rows"]), len(issues))
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
    from stocking_sheet_sync.domain.sheets.layout import _bounds

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
    partial: bool = False,
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
        partial：是否跳过异常平台并填写其余平台。
    返回值：与通用销量回读核验器兼容的请求；共享结构异常阻断全部写入，partial控制平台隔离。
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
    skipped = []
    metrics = [*INPUT_METRICS] if history else []
    if forecast:
        metrics.append("forecast")
    expected_platforms = {
        p["id"]
        for p in config["platforms"]
        if not config.get("selected_platforms") or p["id"] in config["selected_platforms"]
    }
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
                if metric == "forecast" and platform["status"] == "manual":
                    skipped.append(
                        {
                            "style": row["style"],
                            "sku": row["sku"],
                            "platform": pid,
                            "target_cell": address,
                            "reason": platform["issues"],
                            "status": "preserved"
                            if snapshot["cells"][address].get("value") not in (None, "")
                            or snapshot["cells"][address].get("formula")
                            else "blank",
                        }
                    )
                    continue
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
                if cell.get("formula") or (
                    existing not in (None, "") and not equal and not config.get("overwrite")
                ):
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
            if metric == "forecast" and any(e["platform"] == pid for e in skipped):
                continue
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
    LOG.debug("公式预估填充请求生成：entries=%d issues=%d", len(entries), problems)
    result = {
        "as_of": report["as_of"],
        "entries": entries,
        "skipped_forecasts": skipped,
        "total_entries": total_entries,
        "operations": [] if problems else compact_ranges(operations),
        "target_issues": issues,
        "summary": {
            "expected_cells": len(entries),
            "manual_forecasts": len(skipped),
            "needs_review": problems,
            "write": counts["write"],
            "unchanged": counts["unchanged"],
            "platform_totals": dict(totals),
        },
        "status": "needs_review" if problems else "changes_proposed" if operations else "unchanged",
    }

    if partial:
        from stocking_sheet_sync.domain.sheets.platforms import isolate_platforms

        blocked = {}
        for group in report["groups"]:
            for platform in group["platforms"]:
                if platform["status"] == "needs_review":
                    blocked.setdefault(platform["platform"], []).extend(platform["issues"])
        result = isolate_platforms(result, snapshot["sheet_id"], blocked=blocked)
    supplement_demand_totals(
        result,
        snapshot,
        {
            pid: fields[(pid, "demand")]
            for pid in expected_platforms
            if pid not in result.get("blocked_platforms", {})
        },
        [r["row"] for r in checked["rows"]],
    )
    return result

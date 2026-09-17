from __future__ import annotations

import logging
import re
import tomllib
from collections import Counter
from pathlib import Path

from .sheet_matching import column_name, column_number, inspect_sheet, normalize_text

LOG = logging.getLogger(__name__)
HISTORY_METRICS = {"history", "history_net"}


def load_layout_config(path: Path, sales_config: dict) -> dict:
    """
    功能说明：读取市场部表头、款式分类及平台顺序规则，并校验配置一致性。

    参数：
        path：结构规则 TOML 文件路径。
        sales_config：提供平台 ID、表头别名和商品字段的销量配置。

    返回值：通过校验的结构规则字典。
    """
    with path.open("rb") as file:
        rules = tomllib.load(file)
    layout = rules["layout"]
    pattern = re.compile(layout["style_pattern"])
    if "year" not in pattern.groupindex:
        raise ValueError("款式分类正则必须包含 year 分组")
    if type(layout["new_year"]) is not int:
        raise ValueError("新品年份必须是整数")
    group_row = layout["group_row"]
    if type(group_row) is not int or not 1 <= group_row < sales_config["matching"]["header_rows"]:
        raise ValueError("市场部分类行必须位于字段表头之前")
    order = layout["platform_order"]
    ids = {p["id"] for p in sales_config["platforms"]}
    if len(order) != len(set(order)) or set(order) != ids or set(rules["platforms"]) != ids:
        raise ValueError("结构平台顺序必须完整覆盖销量配置中的平台，且不能重复")
    if normalize_text(layout["market_header"]) not in {
        normalize_text(v) for v in sales_config["matching"]["market_headers"]
    }:
        raise ValueError("市场部标题必须属于匹配配置中的市场部别名")
    aliases = {}
    for platform in sales_config["platforms"]:
        pid = platform["id"]
        settings = rules["platforms"][pid]
        for category in ("new", "legacy"):
            metrics = settings.get(f"{category}_metrics", layout[f"{category}_metrics"])
            if (
                not metrics
                or len(metrics) != len(set(metrics))
                or not {"sales", "demand"} <= set(metrics)
                or not set(metrics) <= HISTORY_METRICS | {"sales", "demand"}
                or (category == "new" and set(metrics) & HISTORY_METRICS)
            ):
                raise ValueError(f"平台 {pid} 的 {category} 字段配置无效")
            for metric in metrics:
                if not isinstance(settings.get(metric), str) or not settings[metric].strip():
                    raise ValueError(f"平台 {pid} 缺少 {metric} 标题")
                if metric in HISTORY_METRICS:
                    regex = re.compile(settings[f"{metric}_pattern"])
                    if "period" not in regex.groupindex or settings[metric].count("{period}") != 1:
                        raise ValueError("历史字段必须包含 period 分组和表头占位符")
                    if re.search(r"[{}]", settings[metric].replace("{period}", "")):
                        raise ValueError("历史表头只支持 period 占位符")
        for metric in ("sales", "demand"):
            for label in [settings[metric], *platform[f"{metric}_headers"]]:
                normalized = normalize_text(label)
                if normalized in aliases and aliases[normalized] != (pid, metric):
                    raise ValueError("不同市场部字段的别名不能重叠")
                aliases[normalized] = (pid, metric)
    if not set(rules.get("history_periods", {})) <= ids:
        raise ValueError("历史区间配置包含未知平台")
    if any(
        not isinstance(v, str) or not v.strip() for v in rules.get("history_periods", {}).values()
    ):
        raise ValueError("历史区间配置必须是非空文本")
    if "forecast" in rules:
        forecast = rules["forecast"]
        if forecast.get("metrics") != ["sales", "previous", "future", "forecast", "demand"]:
            raise ValueError("公式预估指标顺序需要包含三项历史、公式预估及人工需求")
        names = [
            forecast.get(key)
            for key in ("company_header", "previous_suffix", "future_suffix", "forecast_header")
        ]
        if any(not isinstance(v, str) or not v.strip() for v in names) or len(set(names)) != 4:
            raise ValueError("公式预估表头配置必须非空且互不重复")
        for template in [forecast["forecast_header"], *forecast.get("forecast_aliases", [])]:
            if not isinstance(template, str) or template.count("{platform}") != 1:
                raise ValueError("预估标题模板必须包含一个platform占位符")
            if re.search(r"[{}]", template.replace("{platform}", "")):
                raise ValueError("预估标题模板只支持platform占位符")
    LOG.info("市场部结构规则加载完成：path=%s platforms=%d", path, len(order))
    return rules


def _recognize(label: str, config: dict, rules: dict) -> list[dict]:
    """识别 label 的平台与指标；config 提供别名，rules 提供标题与区间正则。"""
    normalized = normalize_text(label)
    matches = []
    for platform in config["platforms"]:
        pid = platform["id"]
        titles = rules["platforms"][pid]
        for metric in ("sales", "demand"):
            labels = [titles[metric], *platform[f"{metric}_headers"]]
            if normalized in {normalize_text(v) for v in labels}:
                matches.append({"platform": pid, "metric": metric, "period": None})
        for metric in sorted(HISTORY_METRICS):
            pattern = titles.get(f"{metric}_pattern")
            match = re.fullmatch(pattern, normalized) if pattern else None
            if match:
                matches.append({"platform": pid, "metric": metric, "period": match["period"]})
    forecast = rules.get("forecast", {})
    if normalized == normalize_text(forecast.get("company_header", "")) and normalized:
        matches.append({"platform": "company", "metric": "sales", "period": None})
    for platform in config["platforms"]:
        for metric in ("previous", "future", "forecast"):
            suffix = forecast.get(f"{metric}_suffix")
            labels = (
                [
                    template.format(platform=platform["name"])
                    for template in [
                        forecast["forecast_header"],
                        *forecast.get("forecast_aliases", []),
                    ]
                ]
                if metric == "forecast" and "forecast_header" in forecast
                else [platform["name"] + suffix]
                if suffix
                else []
            )
            if normalized in {normalize_text(value) for value in labels}:
                matches.append({"platform": platform["id"], "metric": metric, "period": None})
    return matches


def _bounds(area: str) -> tuple[int, int, int, int]:
    """把合并范围 area 转为左列、上行、右列、下行。"""
    match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", area)
    if not match:
        raise ValueError("合并范围格式无效")
    left, top, right, bottom = match.groups()
    return column_number(left), int(top), column_number(right), int(bottom)


def plan_market_layout(
    snapshot: dict, config: dict, rules: dict, *, recent_only: bool = False, forecast: bool = False
) -> dict:
    """
    功能说明：生成市场部字段调整预览，保留数量并对无法确定的结构给出阻断原因。

    参数：
        snapshot：完整工作表快照，包含精确坐标、值、公式及合并范围。
        config：商品字段与平台别名配置。
        rules：经过校验的结构规则，包含新品年份和往年区间表头配置。
        recent_only：仅补近30天列，老品已有历史字段原样保留，缺少的历史字段不生成。
        forecast：为老款补齐公式预估所需字段及全公司列，已有历史列保留。

    返回值：款式分类、现有字段、目标字段、拟议操作与问题；不执行表格写入。
    """
    LOG.info("开始市场部结构预览：sheet_id=%s", snapshot["sheet_id"])
    product = inspect_sheet(snapshot, config)
    cells = snapshot["cells"]
    header_row = config["matching"]["header_rows"]
    group_row = rules["layout"]["group_row"]
    issues, warnings, operations, existing, fields = [], [], [], [], []
    style_rows = []
    pattern = re.compile(rules["layout"]["style_pattern"])
    for row in product["rows"]:
        match = pattern.match(str(row["style"] or "").strip())
        year = int(match["year"]) if match and match["year"].isdigit() else None
        target_year = rules["layout"]["new_year"]
        category = (
            "new"
            if year == target_year
            else "legacy"
            if year is not None and year < target_year
            else "unknown"
        )
        style_rows.append(
            {"row": row["row"], "style": row["style"], "sku": row["sku"], "category": category}
        )
        if row["issues"]:
            issues.append(
                {"reason": "product_row_invalid", "row": row["row"], "details": row["issues"]}
            )
    categories = {r["category"] for r in style_rows}
    category = next(iter(categories)) if len(categories) == 1 else "mixed"
    if not style_rows:
        issues.append({"reason": "no_product_rows"})
    elif category not in {"new", "legacy"}:
        issues.append(
            {"reason": "style_classification_unresolved", "categories": sorted(categories)}
        )

    market_names = {normalize_text(v) for v in config["matching"]["market_headers"]}
    markers = [
        c
        for c in range(1, snapshot["column_count"] + 1)
        if normalize_text(cells[f"{column_name(c)}{group_row}"].get("value")) in market_names
    ]
    start = end = None
    original_merge = None
    if len(markers) != 1:
        issues.append(
            {"reason": "market_group_unresolved", "columns": [column_name(c) for c in markers]}
        )
    else:
        start = end = markers[0]
        for area in snapshot["merges"]:
            left, top, right, bottom = _bounds(area)
            if left <= start <= right and top <= group_row <= bottom:
                if top != group_row or bottom != group_row:
                    issues.append({"reason": "market_group_crosses_header_rows", "range": area})
                original_merge = area
                start, end = left, right
                break

        # 只接纳紧邻分组且分类行完全空白的已知字段，不能跨过其他部门标题或合并范围。
        def recoverable(col):
            if not 1 <= col <= snapshot["column_count"]:
                return False
            cell = cells[f"{column_name(col)}{group_row}"]
            if cell.get("value") not in (None, "") or cell.get("formula"):
                return False
            for area in snapshot["merges"]:
                left, top, right, bottom = _bounds(area)
                if left <= col <= right and top <= group_row <= bottom:
                    return False
            return (
                len(
                    _recognize(
                        cells[f"{column_name(col)}{header_row}"].get("value", ""), config, rules
                    )
                )
                == 1
            )

        while recoverable(start - 1):
            start -= 1
        while recoverable(end + 1):
            end += 1
        for c in range(start, end + 1):
            col = column_name(c)
            header = cells[f"{col}{header_row}"]
            label = header.get("value", "")
            matches = _recognize(label, config, rules)
            filled = sum(
                cells[f"{col}{r['row']}"].get("value") not in (None, "")
                or bool(cells[f"{col}{r['row']}"].get("formula"))
                for r in style_rows
            )
            item = {"column": col, "header": label, "filled_product_cells": filled}
            if len(matches) == 1 and not header.get("formula"):
                item.update(matches[0])
            else:
                combined = normalize_text(label) in {
                    normalize_text(v) for v in config["matching"]["combined_headers"]
                }
                issues.append(
                    {
                        "reason": "combined_platform_column"
                        if combined
                        else "unrecognized_or_ambiguous_header",
                        **item,
                    }
                )
            existing.append(item)

    mapped = {}
    for item in existing:
        if "platform" not in item:
            continue
        key = (item["platform"], item["metric"])
        if key in mapped:
            issues.append(
                {
                    "reason": "duplicate_field",
                    "platform": key[0],
                    "metric": key[1],
                    "columns": [mapped[key]["column"], item["column"]],
                }
            )
        else:
            mapped[key] = item

    if category in {"new", "legacy"}:
        if forecast and category != "legacy":
            issues.append({"reason": "forecast_requires_legacy_styles"})
        company = mapped.get(("company", "sales"))
        if company or forecast:
            fields.append(
                {
                    "platform": "company",
                    "platform_name": "全公司",
                    "metric": "sales",
                    "header": rules["forecast"]["company_header"],
                    "period_label": None,
                    "source_column": company["column"] if company else None,
                    "target_column": column_name(start) if start else None,
                    "filled_product_cells": company["filled_product_cells"] if company else 0,
                }
            )
        for pid in rules["layout"]["platform_order"]:
            titles = rules["platforms"][pid]
            metrics = titles.get(f"{category}_metrics", rules["layout"][f"{category}_metrics"])
            periods = {
                v["period"] for (p, m), v in mapped.items() if p == pid and m in HISTORY_METRICS
            }
            if recent_only and category == "legacy":
                metrics = [m for m in metrics if m not in HISTORY_METRICS or (pid, m) in mapped]
            extended = {"previous", "future", "forecast"}
            if forecast:
                metrics = [m for m in metrics if m in HISTORY_METRICS]
                metrics += rules["forecast"]["metrics"]
            elif any((pid, metric) in mapped for metric in extended):
                metrics = [m for m in metrics if m != "demand"]
                metrics += [
                    m for m in rules["forecast"]["metrics"] if m in extended and (pid, m) in mapped
                ]
                metrics.append("demand")
            configured_period = None if recent_only else rules.get("history_periods", {}).get(pid)
            if configured_period:
                periods.add(normalize_text(configured_period))
            if category == "legacy" and not recent_only and len(periods) > 1:
                issues.append(
                    {
                        "reason": "history_period_conflict",
                        "platform": pid,
                        "periods": sorted(periods),
                    }
                )
            period = next(iter(periods)) if len(periods) == 1 else None
            for metric in metrics:
                source = mapped.get((pid, metric))
                title = (
                    source["header"]
                    if source and metric == "demand"
                    else rules["forecast"]["forecast_header"].format(
                        platform=next(p["name"] for p in config["platforms"] if p["id"] == pid)
                    )
                    if metric == "forecast"
                    else next(p["name"] for p in config["platforms"] if p["id"] == pid)
                    + rules["forecast"][f"{metric}_suffix"]
                    if metric in extended
                    else titles[metric]
                )
                if metric in HISTORY_METRICS:
                    title = (
                        source["header"]
                        if source
                        else titles[metric].format(period=period)
                        if period
                        else None
                    )
                    if title is None:
                        issues.append(
                            {"reason": "history_period_required", "platform": pid, "metric": metric}
                        )
                fields.append(
                    {
                        "platform": pid,
                        "platform_name": next(
                            p["name"] for p in config["platforms"] if p["id"] == pid
                        ),
                        "metric": metric,
                        "header": title,
                        "period_label": source.get("period")
                        if source
                        else period
                        if metric in HISTORY_METRICS
                        else None,
                        "source_column": source["column"] if source else None,
                        "target_column": column_name(start + len(fields)) if start else None,
                        "filled_product_cells": source["filled_product_cells"] if source else 0,
                    }
                )
        wanted = {(f["platform"], f["metric"]) for f in fields}
        for key, item in mapped.items():
            if key not in wanted:
                issues.append({"reason": "extra_metric_preserved", **item})

    if not issues:
        sequence = [item["column"] for item in existing]
        for index, field in enumerate(fields):
            source = field["source_column"]
            target = field["target_column"]
            if source is None:
                sequence.insert(index, f"new:{index}")
                operations.append(
                    {
                        "action": "insert_market_column",
                        "position": target,
                        "platform": field["platform"],
                        "metric": field["metric"],
                    }
                )
            else:
                current_index = sequence.index(source)
                if current_index != index:
                    operations.append(
                        {
                            "action": "move_market_column",
                            "source": column_name(start + current_index),
                            "position": target,
                            "original_source_column": source,
                        }
                    )
                    sequence.insert(index, sequence.pop(current_index))
            current = cells[f"{source}{header_row}"].get("value", "") if source else ""
            if current != field["header"]:
                operations.append(
                    {
                        "action": "set_market_field_header",
                        "cell": f"{target}{header_row}",
                        "before": current,
                        "after": field["header"],
                    }
                )
        desired_merge = (
            f"{column_name(start)}{group_row}:{column_name(start + len(fields) - 1)}{group_row}"
        )
        if (
            original_merge != desired_merge
            or cells[f"{column_name(markers[0])}{group_row}"].get("value")
            != rules["layout"]["market_header"]
        ):
            operations.append(
                {
                    "action": "set_market_group_header",
                    "before_range": original_merge or f"{column_name(markers[0])}{group_row}",
                    "after_range": desired_merge,
                    "header": rules["layout"]["market_header"],
                }
            )
        if any(op["action"] == "insert_market_column" for op in operations):
            warnings.append("插入市场部列会使右侧列位置顺移；预览不修改其他部门内容或汇总公式。")

    for field in fields:
        LOG.info(
            "市场部字段预览：platform=%s metric=%s source=%s target=%s filled=%d",
            field["platform"],
            field["metric"],
            field["source_column"],
            field["target_column"],
            field["filled_product_cells"],
        )
    status = "needs_review" if issues else "changes_proposed" if operations else "ready"
    LOG.log(
        logging.WARNING if issues else logging.INFO,
        "市场部结构预览完成：status=%s rows=%d fields=%d operations=%d issues=%d",
        status,
        len(style_rows),
        len(fields),
        len(operations),
        len(issues),
    )
    return {
        "spreadsheet_token": snapshot.get("spreadsheet_token"),
        "title": snapshot.get("title"),
        "sheet_id": snapshot["sheet_id"],
        "revision": snapshot.get("revision"),
        "status": status,
        "category": category,
        "style_rows": style_rows,
        "style_counts": dict(Counter(r["style"] for r in style_rows)),
        "market_range": f"{column_name(start)}:{column_name(end)}" if start else None,
        "existing_fields": existing,
        "target_fields": fields,
        "operations": operations,
        "issues": issues,
        "warnings": warnings,
        "preview_only": True,
    }

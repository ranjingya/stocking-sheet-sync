from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from pathlib import Path

from stocking_sheet_sync.domain.forecast import allocate_forecast
from stocking_sheet_sync.domain.periods import forecast_window
from stocking_sheet_sync.domain.products import match_catalog
from stocking_sheet_sync.domain.sheets.company import read_company_sales
from stocking_sheet_sync.infrastructure.warehouse import ForecastReader

LOG = logging.getLogger(__name__)


def log_platform_result(style: str, platform_name: str, item: dict) -> None:
    """
    功能说明：用中文说明单平台数据检查结果，区分计算完成和实际写入。

    参数：
        style：当前款号。
        platform_name：配置中的平台显示名称。
        item：包含状态、输入、预测和问题的试算结果。
    返回值：无，记录简短业务日志及DEBUG诊断信息。
    """
    inputs = item.get("inputs", {})
    if item["status"] == "needs_review":
        stages = {
            "current": "近30天",
            "previous": "去年同期近30天",
            "historical_future": "去年后续周期",
        }
        missing = [label for key, label in stages.items() if key not in inputs]
        reason = "、".join(missing) + "数据缺失或校验未通过" if missing else "计算数据异常，需核对"
        text = f"历史与预测暂不可填；原因：{reason}"
    else:
        recent = sum(inputs.get("current", {}).values())
        text = f"历史数据可用（近30天{recent}件）；"
        if item["status"] == "ready":
            text += f"预估{item['forecast']['total']}件，待写入"
        elif "previous_sales_zero" in item.get("issues", []):
            text += "预测未计算：去年同期近30天销量为0"
        elif "company_lifecycle_sales_unavailable" in item.get("issues", []):
            text += "预测未计算：触发全公司占比兜底，但表内全公司销量不可用"
        else:
            text += "预测未计算：需人工核对"
    LOG.log(
        logging.INFO if item["status"] == "ready" else logging.WARNING,
        "%s｜%s：%s",
        style,
        platform_name,
        text,
    )
    LOG.debug(
        "平台试算明细：style=%s platform=%s status=%s issues=%s",
        style,
        item["platform"],
        item["status"],
        item.get("issues", []),
    )


def quantities(source: dict, skus: list[str], *, absent_is_zero: bool) -> dict[str, int]:
    """将 source 的完整有效结果映射到 skus；absent_is_zero 控制明细无出库补零。"""
    if source["issues"]:
        raise ValueError("来源校验未通过：" + ",".join(source["issues"]))
    by_sku = defaultdict(list)
    for row in source["rows"]:
        by_sku[row["sku"]].append(row)
    result = {}
    for sku in skus:
        values = by_sku[sku]
        if not values and absent_is_zero:
            result[sku] = 0
        elif len(values) == 1 and values[0]["status"] == "matched":
            result[sku] = values[0]["quantity"]
        else:
            raise ValueError(f"SKU销量缺失或不唯一：{sku}")
    return result


def inspect_forecast(
    reader: ForecastReader,
    styles: list[str],
    as_of: date,
    sales_config: dict,
    sources: dict,
    rules: dict,
    layout_rules: dict,
    *,
    requested_rows: list[dict] | None = None,
    snapshot: dict | None = None,
) -> dict:
    """
    功能说明：逐款逐平台组装真实历史数据，生成只读预测及待核对证据。

    参数：
        reader：本项目独立只读数仓客户端。
        styles：按款号诊断时的试算范围；传入表格商品行时以商品行款号为准。
        as_of：明确指定的预估日，当前窗口不含当天。
        sales_config：现有平台明细及快照配置。
        sources：预测主数据、日快照与全公司来源状态配置。
        rules：季节和分配计算规则。
        layout_rules：款号年份分类规则，新品与未知款不参与自动预测。
        requested_rows：可选需求表商品行，仅按其中SKU查询主数据、标签及销量。
        snapshot：可选原始表格快照，用于读取全公司生命周期销量。
    返回值：款式主数据、日期、来源查询证据、各平台结果及汇总；不写飞书或Redis。
    """
    styles = sorted(set(styles))
    pattern = re.compile(layout_rules["layout"]["style_pattern"])
    requested = None
    if requested_rows is None:
        catalog = reader.styles(sources["catalog"], styles)
    else:
        requested = deepcopy(requested_rows)
        if not requested:
            raise ValueError("需求表没有待查询商品")
        styles = sorted({str(row["style"]) for row in requested})
        wanted = sorted(
            {row["sku"] for row in requested if isinstance(row["sku"], str) and row["sku"]}
        )
        catalog = reader.catalog(sources["catalog"], wanted)
        catalog = [record for record in catalog if record["sku"] in wanted]
        match_catalog({"rows": requested}, catalog)
        LOG.debug("按需求表读取主数据：rows=%d skus=%d", len(requested), len(wanted))
    company_source = read_company_sales(snapshot, requested or [], rules)
    sku_styles = defaultdict(set)
    for record in catalog:
        sku_styles[record["sku"]].add(record["style"])
    groups = []
    for style in styles:
        LOG.debug("开始款式预测试算：style=%s as_of=%s", style, as_of)
        records = [r for r in catalog if r["style"] == style]
        skus = sorted({r["sku"] for r in records if isinstance(r["sku"], str) and r["sku"]})
        group = {"style": style, "skus": skus, "catalog": records, "issues": [], "platforms": []}
        groups.append(group)
        if requested is not None:
            for row in requested:
                if str(row["style"]) == style and row["issues"]:
                    group["issues"].append(f"{row['sku']}: " + ",".join(row["issues"]))
        match = pattern.match(style)
        if (
            not match
            or not match["year"].isdigit()
            or int(match["year"]) >= (layout_rules["layout"]["new_year"])
        ):
            group["issues"].append("not_legacy_style")
        if not records:
            group["issues"].append("style_not_found")
        if any(len(sku_styles[sku]) > 1 for sku in skus):
            group["issues"].append("sku_assigned_to_multiple_styles")
        if len(skus) != len(records):
            group["issues"].append("catalog_identity_or_labels_conflict")
        if any(not r.get("name") or not r.get("spec") for r in records):
            group["issues"].append("catalog_identity_incomplete")
        try:
            window = forecast_window(as_of, [r["labels"] for r in records], rules)
            group["window"] = {key: str(value) for key, value in window.items()}
        except ValueError as error:
            group["issues"].append(str(error))
        if group["issues"]:
            group["status"] = "needs_review"
            LOG.warning("款式主数据待核对：style=%s issues=%s", style, group["issues"])
            continue
        for platform in sales_config["platforms"]:
            pid = platform["id"]
            item = {"platform": pid, "status": "needs_review", "issues": [], "sources": {}}
            group["platforms"].append(item)
            data = {}
            for key, start, stop in (
                ("current", window["current_start"], window["current_end"]),
                ("previous", window["previous_start"], window["previous_end"]),
                ("historical_future", window["history_start"], window["history_end"]),
            ):
                try:
                    daily = sources.get("daily", {}).get(pid)
                    selected = dict(daily or platform)
                    if key in {"current", "previous"} and platform.get("summary"):
                        selected.update(
                            summary=platform["summary"], summary_metric=key, summary_as_of=as_of
                        )
                    source = (
                        reader.daily_window(selected, skus, start, stop)
                        if daily
                        else reader.sales_window(selected, skus, start, stop)
                    )
                    item["sources"][key] = source
                    data[key] = quantities(
                        source, skus, absent_is_zero=not daily and platform["kind"] == "detail"
                    )
                except (ValueError, RuntimeError) as error:
                    item["issues"].append(f"{key}: {error}")
            item["inputs"] = data
            if not item["issues"]:
                fallback = rules["fallback"]
                if sum(data["previous"].values()) == 0:
                    item["status"] = "manual"
                    item["issues"].append("previous_sales_zero")
                else:
                    use_company = (
                        len(skus) < fallback["sku_count_below"]
                        and sum(data["current"].values()) < fallback["sales_below"]
                    )
                    company = None
                    if use_company:
                        company_rows = [r for r in company_source["rows"] if r["sku"] in skus]
                        if (
                            company_source["status"] == "available"
                            and len(company_rows) == len(skus)
                            and all(r["quantity"] is not None for r in company_rows)
                        ):
                            company = {r["sku"]: r["quantity"] for r in company_rows}
                        if company is None or sum(company.values()) == 0:
                            item["status"] = "manual"
                            item["issues"].append("company_lifecycle_sales_unavailable")
                    if item["status"] != "manual":
                        try:
                            item["forecast"] = allocate_forecast(
                                data["current"],
                                sum(data["previous"].values()),
                                sum(data["historical_future"].values()),
                                company,
                                rules,
                            )
                            item["status"] = "ready"
                        except ValueError as error:
                            item["issues"].append(str(error))
            log_platform_result(style, platform.get("name", pid), item)
        statuses = {p["status"] for p in group["platforms"]}
        group["status"] = (
            "needs_review"
            if "needs_review" in statuses
            else "manual"
            if "manual" in statuses
            else "ready"
        )
    summary = Counter(p["status"] for g in groups for p in g["platforms"])
    return {
        "as_of": str(as_of),
        "mode": "read_only_requested_skus"
        if requested is not None
        else "read_only_whole_style_trial",
        "company_source": company_source,
        "groups": groups,
        "summary": {
            "styles": len(groups),
            "styles_needing_review": sum(g["status"] == "needs_review" for g in groups),
            "styles_manual": sum(g["status"] == "manual" for g in groups),
            "platform_results": sum(summary.values()),
            **summary,
        },
    }


def write_forecast_report(output: Path, report: dict) -> None:
    """将 report 的全部证据及SKU结果保存至 output，返回空值。"""
    output.mkdir(parents=True, exist_ok=True)
    (output / "forecast.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    columns = [
        "style",
        "platform",
        "sku",
        "status",
        "current",
        "previous",
        "historical_future",
        "share_source",
        "share",
        "rounded",
        "adjustment",
        "forecast",
        "issues",
    ]
    with (output / "forecast.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for group in report["groups"]:
            for platform in group["platforms"] or [{"platform": "", "status": "needs_review"}]:
                forecast = platform.get("forecast", {})
                for sku in group["skus"] or [""]:
                    result = forecast.get("rows", {}).get(sku, {})
                    writer.writerow(
                        {
                            "style": group["style"],
                            "platform": platform["platform"],
                            "sku": sku,
                            "status": platform["status"],
                            **{
                                key: platform.get("inputs", {}).get(key, {}).get(sku)
                                for key in ("current", "previous", "historical_future")
                            },
                            "share_source": forecast.get("share_source"),
                            "share": result.get("share"),
                            "rounded": result.get("rounded"),
                            "adjustment": result.get("adjustment"),
                            "forecast": result.get("quantity"),
                            "issues": ";".join([*group["issues"], *platform.get("issues", [])]),
                        }
                    )
    LOG.debug("预测试算报告已保存：output=%s summary=%s", output, report["summary"])

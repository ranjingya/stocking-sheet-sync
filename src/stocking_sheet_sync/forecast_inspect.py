from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from pathlib import Path

from .company_sales import read_company_sales
from .forecast import allocate_forecast, forecast_window, load_forecast_config
from .forecast_reader import ForecastReader, load_forecast_sources
from .logging_config import configure_logging
from .sales_config import WarehouseSettings, load_sales_config
from .sheet_layout import load_layout_config
from .sheet_matching import inspect_sheet, match_catalog

LOG = logging.getLogger(__name__)


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
        LOG.info("按需求表读取主数据：rows=%d skus=%d", len(requested), len(wanted))
    company_source = read_company_sales(snapshot, requested or [], rules)
    sku_styles = defaultdict(set)
    for record in catalog:
        sku_styles[record["sku"]].add(record["style"])
    groups = []
    for style in styles:
        LOG.info("开始款式预测试算：style=%s as_of=%s", style, as_of)
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
                    source = (
                        reader.daily_window(daily, skus, start, stop)
                        if daily
                        else reader.sales_window(platform, skus, start, stop)
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
            LOG.log(
                logging.INFO if item["status"] == "ready" else logging.WARNING,
                "平台试算结束：style=%s platform=%s status=%s issues=%s",
                style,
                pid,
                item["status"],
                item["issues"],
            )
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
    LOG.info("预测试算报告已保存：output=%s summary=%s", output, report["summary"])


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：执行独立的整款只读数仓试算并生成本地JSON和CSV。

    参数：
        argv：命令行参数；空值时读取进程参数。
    返回值：全部可计算返回0，存在待核对返回2，运行或配置失败返回1。
    """
    parser = argparse.ArgumentParser(description="只读试算老款需求，不修改飞书表格")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--style", action="append", help="按款号诊断，可重复传入")
    scope.add_argument("--snapshot", type=Path, help="按本地下单表快照中的SKU试算")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-config", type=Path, default=Path("config/sales-sources.toml"))
    parser.add_argument(
        "--forecast-sources", type=Path, default=Path("config/forecast-sources.toml")
    )
    parser.add_argument("--rules", type=Path, default=Path("config/forecast.toml"))
    parser.add_argument("--layout-config", type=Path, default=Path("config/sheet-layout.toml"))
    parser.add_argument("--db-env-file", type=Path)
    args = parser.parse_args(argv)
    configure_logging("INFO")
    try:
        config = load_sales_config(args.source_config)
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
        requested_rows = inspect_sheet(snapshot, config)["rows"] if snapshot else None
        report = inspect_forecast(
            ForecastReader(WarehouseSettings.load(args.db_env_file)),
            args.style or [],
            args.as_of,
            config,
            load_forecast_sources(args.forecast_sources),
            load_forecast_config(args.rules),
            load_layout_config(args.layout_config, config),
            requested_rows=requested_rows,
            snapshot=snapshot,
        )
        write_forecast_report(args.output, report)
        return (
            2
            if report["summary"]["styles_needing_review"] or report["summary"]["styles_manual"]
            else 0
        )
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as error:
        LOG.error("预测试算未完成：%s", error)
        return 1


def main() -> None:
    raise SystemExit(run())

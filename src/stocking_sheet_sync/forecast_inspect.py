from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .forecast import allocate_forecast, forecast_window, load_forecast_config
from .forecast_reader import ForecastReader, load_forecast_sources
from .logging_config import configure_logging
from .sales_config import WarehouseSettings, load_sales_config
from .sheet_layout import load_layout_config

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
) -> dict:
    """
    功能说明：逐款逐平台组装真实历史数据，生成只读预测及待核对证据。

    参数：
        reader：本项目独立只读数仓客户端。
        styles：待试算款号，整款SKU从主数据获取。
        as_of：明确指定的预估日，当前窗口不含当天。
        sales_config：现有平台明细及快照配置。
        sources：预测主数据、日快照与全公司来源状态配置。
        rules：季节和分配计算规则。
        layout_rules：款号年份分类规则，新品与未知款不参与自动预测。
    返回值：款式主数据、日期、来源查询证据、各平台结果及汇总；不写飞书或Redis。
    """
    styles = sorted(set(styles))
    pattern = re.compile(layout_rules["layout"]["style_pattern"])
    catalog = reader.styles(sources["catalog"], styles)
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
                if len(skus) < fallback["sku_count_below"] and (
                    sum(data["current"].values()) < fallback["sales_below"]
                ):
                    item["issues"].append("company_scope_pending_confirmation")
                else:
                    try:
                        item["forecast"] = allocate_forecast(
                            data["current"],
                            sum(data["previous"].values()),
                            sum(data["historical_future"].values()),
                            None,
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
        group["status"] = (
            "ready" if all(p["status"] == "ready" for p in group["platforms"]) else "needs_review"
        )
    summary = Counter(p["status"] for g in groups for p in g["platforms"])
    return {
        "as_of": str(as_of),
        "mode": "read_only_whole_style_trial",
        "company_source": sources["company"],
        "groups": groups,
        "summary": {
            "styles": len(groups),
            "styles_needing_review": sum(g["status"] == "needs_review" for g in groups),
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
    parser.add_argument("--style", action="append", required=True, help="款号，可重复传入")
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
        report = inspect_forecast(
            ForecastReader(WarehouseSettings.load(args.db_env_file)),
            args.style,
            args.as_of,
            config,
            load_forecast_sources(args.forecast_sources),
            load_forecast_config(args.rules),
            load_layout_config(args.layout_config, config),
        )
        write_forecast_report(args.output, report)
        return 2 if report["summary"]["styles_needing_review"] else 0
    except (ValueError, RuntimeError, KeyError, TypeError, OSError) as error:
        LOG.error("预测试算未完成：%s", error)
        return 1


def main() -> None:
    raise SystemExit(run())

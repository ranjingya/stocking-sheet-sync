from __future__ import annotations

import logging
import tomllib
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path

LOG = logging.getLogger(__name__)


def load_forecast_config(path: Path = Path("config/forecast.toml")) -> dict:
    """读取并校验 path 指定的季节及兜底规则，返回配置字典。"""
    with path.open("rb") as stream:
        rules = tomllib.load(stream)
    seasons = rules["seasons"]
    labels = []
    for name in ("summer", "winter", "all_year"):
        values = seasons[f"{name}_labels"]
        if not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("季节标签必须为非空字符串列表")
        labels.extend(values)
    if len(labels) != len(set(labels)):
        raise ValueError("季节标签不能重复或跨季节配置")
    for name in ("summer_end", "winter_end"):
        month, day = seasons[name]
        date(2001, month, day)
    for name in ("sku_count_below", "sales_below"):
        value = rules["fallback"][name]
        if type(value) is not int or value <= 0:
            raise ValueError("兜底阈值必须为正整数")
    LOG.info("预测规则加载完成：path=%s", path)
    return rules


def previous_year(day: date) -> date:
    """将 day 映射到去年同日；闰日返回去年2月28日。"""
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def forecast_window(as_of: date, labels: list[str | None], rules: dict) -> dict:
    """
    功能说明：识别同款季节并生成当前和去年观察窗口、后续需求窗口。

    参数：
        as_of：本次预估日，当前销量不包含该日，未来需求包含该日。
        labels：该款主数据标签集合，每项为逗号分隔文本或空值。
        rules：经过校验的外部预测规则。
    返回值：季节及日期对象组成的字典，所有区间右端均为排除边界；冲突抛错。
    """
    seasons = rules["seasons"]
    kinds = set()
    for raw in labels or [None]:
        tokens = {v.strip() for v in (raw or "").split(",") if v.strip()}
        matches = {
            k for k in ("summer", "winter", "all_year") if tokens & set(seasons[f"{k}_labels"])
        }
        if len(matches) > 1:
            raise ValueError("同一记录存在多个季节标签")
        kinds.update(matches or {"all_year"})
    if len(kinds) != 1:
        raise ValueError("同款主数据季节不一致，请核对标签及历史记录")
    season = next(iter(kinds))
    summer = date(as_of.year, *seasons["summer_end"])
    winter = date(as_of.year, *seasons["winter_end"])
    if season == "summer":
        end = summer
    elif season == "winter":
        end = winter if as_of <= winter else date(as_of.year + 1, *seasons["winter_end"])
    else:
        end = min(
            d
            for d in (
                winter,
                summer,
                date(as_of.year + 1, *seasons["winter_end"]),
                date(as_of.year + 1, *seasons["summer_end"]),
            )
            if d >= as_of
        )
    if end < as_of:
        raise ValueError("下单日期已超过季末，跨季下单需人工核对")
    prior = previous_year(as_of)
    result = {
        "season": season,
        "current_start": as_of - timedelta(days=30),
        "current_end": as_of,
        "previous_start": prior - timedelta(days=30),
        "previous_end": prior,
        "future_start": as_of,
        "future_end": end + timedelta(days=1),
        "history_start": prior,
        "history_end": previous_year(end) + timedelta(days=1),
        "deadline": end,
    }
    LOG.info("预测日期确定：season=%s as_of=%s deadline=%s", season, as_of, end)
    return result


def allocate_forecast(
    current: dict[str, int],
    previous: int,
    historical_future: int,
    company: dict[str, int] | None,
    rules: dict,
) -> dict:
    """
    功能说明：计算单平台单款老品同比需求、SKU占比和整件分配。

    参数：
        current：本次查询范围内的同款SKU集合及当前30天平台实发量。
        previous：该平台该款去年同期30天实发总量，必须大于零。
        historical_future：该平台该款去年对应后续周期实发总量。
        company：可选全公司同款相同SKU范围的近30天量，仅用于占比兜底。
        rules：经过校验的预测规则，包含严格小于阈值。
    返回值：款式总量、占比来源、逐SKU四舍五入及差额调整证据；无效输入抛错。
    """
    if not current or any(not isinstance(k, str) or not k for k in current):
        raise ValueError("需要完整且有效的SKU集合")
    values = [*current.values(), previous, historical_future]
    if any(type(v) is not int or v < 0 for v in values):
        raise ValueError("预测输入必须为非负整数件数")
    if previous == 0:
        raise ValueError("去年同期销量为零，不能计算同比")
    total_sales = sum(current.values())
    fallback = rules["fallback"]
    use_company = (
        len(current) < fallback["sku_count_below"] and total_sales < fallback["sales_below"]
    )
    weights = current
    if use_company:
        if company is None or set(company) != set(current):
            raise ValueError("全公司兜底需要相同的完整SKU集合")
        if any(type(v) is not int or v < 0 for v in company.values()):
            raise ValueError("全公司件数必须为非负整数")
        weights = company
    denominator = sum(weights.values())
    if denominator == 0:
        raise ValueError("占比参考销量为零，无法分配需求")
    exact_total = Decimal(historical_future) * total_sales / previous
    total = int(exact_total.to_integral_value(rounding=ROUND_CEILING))
    rows = {}
    order = sorted(weights, key=lambda k: (-weights[k], k))
    for sku in order:
        exact = Decimal(total) * weights[sku] / denominator
        rounded = int(exact.to_integral_value(rounding=ROUND_HALF_UP))
        rows[sku] = {
            "weight": weights[sku],
            "share": str(Decimal(weights[sku]) / denominator),
            "unrounded": str(exact),
            "rounded": rounded,
            "adjustment": 0,
            "quantity": rounded,
        }
    difference = total - sum(r["quantity"] for r in rows.values())
    remaining = difference
    for sku in order:
        change = remaining if remaining >= 0 else -min(-remaining, rows[sku]["quantity"])
        rows[sku]["adjustment"] = change
        rows[sku]["quantity"] += change
        remaining -= change
        if remaining == 0:
            break
    if remaining or sum(r["quantity"] for r in rows.values()) != total:
        raise ValueError("SKU分配合计校验失败")
    LOG.info(
        "老款需求计算完成：skus=%d total=%d share_source=%s adjustment=%d",
        len(rows),
        total,
        "company" if use_company else "platform",
        difference,
    )
    return {
        "current_total": total_sales,
        "previous_total": previous,
        "historical_future": historical_future,
        "unrounded_total": str(exact_total),
        "total": total,
        "share_source": "company" if use_company else "platform",
        "rounding_difference": difference,
        "rows": rows,
    }

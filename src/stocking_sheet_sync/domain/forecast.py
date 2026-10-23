from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

LOG = logging.getLogger(__name__)


def trend_factor(total: int, days15: int, days7: int) -> Decimal:
    """根据30天total、15天days15、7天days7实发量，返回70/20/10加权且限幅的趋势系数。"""
    if any(type(v) is not int or v < 0 for v in (total, days15, days7)):
        raise ValueError("趋势窗口必须为非负整数件数")
    if not days7 <= days15 <= total:
        raise ValueError("趋势窗口数量不满足7天≤15天≤30天，请核对数据来源")
    if total == 0:
        return Decimal(1)
    weighted = (
        Decimal("0.7") * total / 30 + Decimal("0.2") * days15 / 15 + Decimal("0.1") * days7 / 7
    )
    return min(Decimal("1.2"), max(Decimal("0.8"), weighted / (Decimal(total) / 30)))


def allocate_forecast(
    current: dict[str, int],
    previous: int,
    historical_future: int,
    company: dict[str, int] | None,
    rules: dict,
    trend: dict[str, int] | None = None,
) -> dict:
    """
    功能说明：计算趋势修正且限幅的单平台单款需求、SKU占比和整倍数四舍五入。

    参数：
        current：本次查询范围内的同款SKU集合及当前30天平台实发量。
        previous：该平台该款去年同期30天实发总量，必须大于零。
        historical_future：该平台该款去年对应后续周期实发总量。
        company：可选表内全公司同款相同SKU范围的近30天销量，仅用于占比兜底。
        rules：经过校验的预测规则，包含倍率上限、严格小于阈值和取整单位。
        trend：今年和去年15天、7天数量；省略时使用中性趋势系数。
    返回值：款式总量、占比来源、逐SKU四舍五入证据；无效输入抛错。
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
    cap = Decimal(str(rules.get("growth_multiplier_cap", 1.5)))
    if not cap.is_finite() or cap < 1:
        raise ValueError("预测增长倍率上限必须为大于等于1的有限数值")
    current_trend = previous_trend = Decimal(1)
    if trend is not None:
        current_trend = trend_factor(total_sales, trend["current_15"], trend["current_7"])
        previous_trend = trend_factor(previous, trend["previous_15"], trend["previous_7"])
    multiplier = Decimal(total_sales) / previous * current_trend / previous_trend
    capped = multiplier > cap
    exact_total = (
        Decimal(historical_future) * cap
        if capped
        else Decimal(historical_future) * total_sales / previous * current_trend / previous_trend
    )
    LOG.debug(
        "预测倍率校验完成：current=%d previous=%d cap=%s capped=%s",
        total_sales,
        previous,
        cap,
        capped,
    )
    unit = rules.get("rounding_unit", 10)
    if type(unit) is not int or unit <= 0:
        raise ValueError("预测取整单位必须为正整数")
    rows = {}
    order = sorted(weights, key=lambda k: (-weights[k], k))
    for sku in order:
        exact = exact_total * weights[sku] / denominator
        rounded = int((exact / unit).to_integral_value(rounding=ROUND_HALF_UP)) * unit
        rows[sku] = {
            "weight": weights[sku],
            "share": str(Decimal(weights[sku]) / denominator),
            "unrounded": str(exact),
            "rounded": rounded,
            "adjustment": 0,
            "quantity": rounded,
        }
    total = sum(r["quantity"] for r in rows.values())
    difference = Decimal(total) - exact_total
    LOG.debug(
        "老款需求计算完成：skus=%d total=%d share_source=%s rounding_difference=%s",
        len(rows),
        total,
        "company" if use_company else "platform",
        difference,
    )
    return {
        "current_trend": str(current_trend),
        "previous_trend": str(previous_trend),
        "final_multiplier": str(min(multiplier, cap)),
        "current_total": total_sales,
        "previous_total": previous,
        "historical_future": historical_future,
        "unrounded_total": str(exact_total),
        "total": total,
        "share_source": "company" if use_company else "platform",
        "rounding_difference": str(difference),
        "rows": rows,
    }

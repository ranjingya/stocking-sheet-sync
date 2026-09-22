from __future__ import annotations

import logging
from datetime import date, timedelta

LOG = logging.getLogger(__name__)


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
    LOG.debug("预测日期确定：season=%s as_of=%s deadline=%s", season, as_of, end)
    return result

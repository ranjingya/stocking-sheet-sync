from __future__ import annotations

import logging
import tomllib
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from .sales_config import identifier
from .sales_reader import SalesReader, _filters, units

LOG = logging.getLogger(__name__)


def load_forecast_sources(path: Path) -> dict:
    """校验 path 中预测来源的标识符和筛选，返回配置字典。"""
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    for source in [config["catalog"], *config.get("daily", {}).values()]:
        identifier(source["table"])
        for field in [*source["fields"].values(), *source.get("rolling_fields", {}).values()]:
            identifier(field)
        for field, values in source.get("filters", {}).items():
            identifier(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) or not v for v in values)
            ):
                raise ValueError("预测来源筛选必须是非空字符串数组")
    if not {"sku", "style", "name", "spec", "labels"} <= config["catalog"]["fields"].keys():
        raise ValueError("预测商品主数据字段不完整")
    for source in config.get("daily", {}).values():
        if (
            source["kind"] != "daily"
            or not {"sku", "date", "quantity", "row_id", "rolling_30"} <= source["fields"].keys()
        ):
            raise ValueError("预测日快照字段不完整")
        for days in source.get("rolling_fields", {}):
            if not str(days).isdigit() or not 1 <= int(days) <= 366:
                raise ValueError("滚动周期必须为1至366天")
        if type(source.get("business_date_offset_days")) is not int:
            raise ValueError("日快照必须明确业务日期偏移")
    return config


class ForecastReader(SalesReader):
    def styles(self, source: dict, styles: list[str]) -> list[dict]:
        """
        功能说明：为按款号诊断读取该款SKU身份和标签。

        参数：
            source：商品主数据来源、字段及筛选配置。
            styles：需要读取的款号列表。
        返回值：完整投影去重后的主数据行；不同身份或标签保留供冲突校验。
        """
        self._validate_skus(styles)
        if not styles:
            return []
        fields = source["fields"]
        condition, params = _filters(source)
        projection = ", ".join(
            f"{identifier(value)} AS {identifier(key)}" for key, value in fields.items()
        )
        rows = self._read(
            f"SELECT DISTINCT {projection} FROM {identifier(source['table'])} "
            f"WHERE {condition} AND {identifier(fields['style'])} "
            f"IN ({','.join(['%s'] * len(styles))})",
            tuple([*params, *styles]),
        )
        LOG.info("整款主数据读取完成：styles=%d rows=%d", len(styles), len(rows))
        return rows

    def daily_window(self, source: dict, skus: list[str], start: date, stop: date) -> dict:
        """
        功能说明：固定周期读取截止日滚动值，其余周期累加日出库并核验覆盖。

        参数：
            source：日快照字段、筛选及已核对的业务日期偏移配置。
            skus：本次需要查询的SKU编码集合。
            start：包含的业务起始日期。
            stop：不包含的业务结束日期。
        返回值：逐SKU数量及实际取数方式；缺失、重复或非法值对应数量留空。
        """
        self._validate_skus(skus)
        days_count = (stop - start).days
        if not skus or len(set(skus)) != len(skus) or not 1 <= days_count <= 366:
            raise ValueError("日快照需要非空唯一SKU集合及1至366天区间")
        rolling_field = source.get("rolling_fields", {}).get(str(days_count))
        if rolling_field:
            return self.rolling_window(source, skus, start, stop, rolling_field)
        offset = timedelta(days=source["business_date_offset_days"])
        fields = source["fields"]
        condition, params = _filters(source)
        projection = ", ".join(
            f"{identifier(value)} AS {identifier(key)}" for key, value in fields.items()
        )
        LOG.info("开始读取日出库：platform=%s start=%s stop=%s", source["id"], start, stop)
        raw = self._read(
            f"SELECT {projection} FROM {identifier(source['table'])} WHERE {condition} "
            f"AND {identifier(fields['date'])} >= %s AND {identifier(fields['date'])} < %s "
            f"AND {identifier(fields['sku'])} IN ({','.join(['%s'] * len(skus))})",
            tuple([*params, (start - offset).isoformat(), (stop - offset).isoformat(), *skus]),
        )
        by_sku = defaultdict(lambda: defaultdict(list))
        for row in raw:
            day = date.fromisoformat(str(row["date"])[:10]) + offset
            if start <= day < stop and str(row["sku"]) in skus:
                by_sku[str(row["sku"])][day].append(row)
        expected = [start + timedelta(days=n) for n in range(days_count)]
        rows = []
        for sku in skus:
            days = by_sku[sku]
            missing = [str(day) for day in expected if day not in days]
            duplicates = [str(day) for day, values in days.items() if len(values) != 1]
            issues = []
            if missing:
                issues.append("incomplete_sku_daily_coverage")
            if duplicates:
                issues.append("duplicate_source_sku_day")
            quantity, rolling = None, None
            if not issues:
                try:
                    if any(values[0]["row_id"] in (None, "") for values in days.values()):
                        raise ValueError("日快照缺少平台SKU标识")
                    quantity = sum(units(days[day][0]["quantity"]) for day in expected)
                except ValueError:
                    issues.append("invalid_daily_value")
            rows.append(
                {
                    "sku": sku,
                    "quantity": quantity if not issues else None,
                    "observed_daily_sum": quantity,
                    "rolling_30": rolling,
                    "status": "matched" if not issues else "needs_review",
                    "issues": issues,
                    "missing_dates": missing,
                    "duplicate_dates": duplicates,
                }
            )
        result = {
            "platform": source["id"],
            "source_table": source["table"],
            "kind": "daily",
            "start": start.isoformat(),
            "end": (stop - timedelta(days=1)).isoformat(),
            "rows": rows,
            "issues": ["daily_source_needs_review"] if any(r["issues"] for r in rows) else [],
        }
        LOG.info("日出库读取完成：platform=%s issues=%s", source["id"], result["issues"])
        return result

    def rolling_window(
        self, source: dict, skus: list[str], start: date, stop: date, field: str
    ) -> dict:
        """
        功能说明：仅读取区间截止日的固定周期销量，不检查或累计中间日期。

        参数：
            source：日快照来源及业务日期偏移配置。
            skus：需要查询的唯一SKU集合。
            start：包含的业务起始日期，用于记录窗口。
            stop：不包含的业务结束日期。
            field：经配置选择的固定周期销量字段。
        返回值：逐SKU截止日滚动销量和缺失、重复、非法值证据。
        """
        fields = source["fields"]
        end = stop - timedelta(days=1)
        day = end - timedelta(days=source["business_date_offset_days"])
        condition, params = _filters(source)
        LOG.info(
            "读取固定周期销量：platform=%s days=%d end=%s field=%s",
            source["id"],
            (stop - start).days,
            end,
            field,
        )
        raw = self._read(
            f"SELECT {identifier(fields['sku'])} AS sku, "
            f"{identifier(fields['row_id'])} AS row_id, {identifier(field)} AS quantity "
            f"FROM {identifier(source['table'])} WHERE {condition} "
            f"AND {identifier(fields['date'])} >= %s AND {identifier(fields['date'])} < %s "
            f"AND {identifier(fields['sku'])} IN ({','.join(['%s'] * len(skus))})",
            tuple([*params, day.isoformat(), (day + timedelta(days=1)).isoformat(), *skus]),
        )
        grouped = defaultdict(list)
        for row in raw:
            grouped[str(row["sku"])].append(row)
        rows = []
        for sku in skus:
            records = grouped[sku]
            issues, quantity = [], None
            if not records:
                issues.append("missing_rolling_snapshot")
            elif len(records) != 1:
                issues.append("duplicate_source_sku_day")
            else:
                try:
                    if records[0]["row_id"] in (None, ""):
                        raise ValueError("快照缺少平台SKU标识")
                    quantity = units(records[0]["quantity"])
                except ValueError:
                    issues.append("invalid_rolling_value")
            rows.append(
                {
                    "sku": sku,
                    "quantity": quantity,
                    "issues": issues,
                    "status": "needs_review" if issues else "matched",
                }
            )
        result = {
            "platform": source["id"],
            "source_table": source["table"],
            "kind": "rolling",
            "source_field": field,
            "start": str(start),
            "end": str(end),
            "rows": rows,
            "issues": ["rolling_source_needs_review"] if any(r["issues"] for r in rows) else [],
        }
        LOG.info("固定周期销量读取完成：platform=%s issues=%s", source["id"], result["issues"])
        return result

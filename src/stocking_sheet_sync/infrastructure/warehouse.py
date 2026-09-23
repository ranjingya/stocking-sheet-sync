from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, timedelta

import pymysql

from stocking_sheet_sync.domain.products import units
from stocking_sheet_sync.settings import WarehouseSettings, identifier

LOG = logging.getLogger(__name__)


def _filters(source: dict) -> tuple[str, list]:
    clauses, values = [], []
    for name, items in source.get("filters", {}).items():
        clauses.append(f"{identifier(name)} IN ({','.join(['%s'] * len(items))})")
        values.extend(items)
    return " AND ".join(clauses) or "1=1", values


class SalesReader:
    def __init__(self, settings: WarehouseSettings):
        self.settings = settings

    def _read(self, sql: str, params: tuple) -> list[dict]:
        """
        功能说明：按次建立连接并执行内部生成的只读参数化查询。

        参数：
            sql：以 SELECT 开头、由可信构造器生成的 SQL。
            params：作为绑定值传入的商品编码、日期和筛选条件。

        返回值：字典行列表；异常经过脱敏，连接始终关闭。
        """
        if not sql.lstrip().upper().startswith("SELECT "):
            raise ValueError("数仓读取器只接受 SELECT 查询")
        config, connection = self.settings, None
        try:
            connection = pymysql.connect(
                host=config.host,
                port=config.port,
                user=config.user,
                password=config.password,
                database=config.database,
                connect_timeout=config.connect_timeout,
                read_timeout=config.read_timeout,
                write_timeout=config.read_timeout,
                charset="utf8mb4",
                autocommit=True,
                cursorclass=pymysql.cursors.DictCursor,
            )
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except pymysql.MySQLError as error:
            code = error.args[0] if error.args and isinstance(error.args[0], int) else None
            LOG.error("数仓读取失败：error_code=%s", code)
            raise RuntimeError(
                f"数仓读取失败，请检查连接、权限或字段配置（错误码 {code}）"
            ) from None
        finally:
            if connection is not None:
                connection.close()

    def catalog(self, source: dict, skus: list[str]) -> list[dict]:
        """
        功能说明：查询指定 SKU 的商品主数据供身份核对。

        参数：
            source：商品主数据的表、字段及筛选配置。
            skus：需要精确匹配的商品编码列表。

        返回值：完整身份字段去重后的主数据行，冲突身份由匹配阶段判断歧义。
        """
        if not skus:
            return []
        self._validate_skus(skus)
        fields = source["fields"]
        condition, params = _filters(source)
        projection = ", ".join(
            f"{identifier(value)} AS {identifier(key)}" for key, value in fields.items()
        )
        sql = (
            f"SELECT DISTINCT {projection} FROM {identifier(source['table'])} WHERE {condition} "
            f"AND {identifier(fields['sku'])} IN ({','.join(['%s'] * len(skus))})"
        )
        rows = self._read(sql, tuple([*params, *skus]))
        LOG.debug("商品主数据读取完成：requested=%d rows=%d", len(skus), len(rows))
        return rows

    def sales(self, source: dict, skus: list[str], as_of: date) -> dict:
        """
        功能说明：按平台读取预估日前 30 个完整自然日的发货件数和覆盖证据。

        参数：
            source：平台来源、发货字段、业务筛选与唯一键配置。
            skus：需要查询的商品编码列表。
            as_of：预估日，不包含该日的数据。

        返回值：统一统计区间、逐 SKU 数据、来源日期和覆盖异常。
        """
        return self.sales_window(
            {**source, "summary_metric": "current", "summary_as_of": as_of},
            skus,
            as_of - timedelta(days=30),
            as_of,
        )

    def summary_window(self, source: dict, skus: list[str], start: date, stop: date) -> dict:
        """
        功能说明：读取基准日对应ADS快照中的今年或去年同期近30天字段。

        参数：
            source：平台配置，含summary字段映射、summary_metric指标和summary_as_of基准日。
            skus：需求表SKU集合。
            start：指标包含的起始日期。
            stop：指标不包含的结束日期。
        返回值：逐SKU数量及来源日期证据；缺失记录保持未知，不记零。
        """
        if (stop - start).days != 30:
            raise ValueError("ADS汇总只支持30天窗口")
        summary = source["summary"]
        metric = source["summary_metric"]
        if metric not in {"current", "previous"}:
            raise ValueError("ADS指标必须为current或previous")
        as_of = source["summary_as_of"]
        selected = {
            "id": source["id"],
            "kind": "snapshot",
            "table": summary["table"],
            "fields": {
                "sku": summary["sku"],
                "date": summary["date"],
                "row_id": summary["row_id"],
                "quantity": summary[metric],
            },
        }
        result = self.sales_window(selected, skus, as_of - timedelta(days=30), as_of)
        found = {r["sku"] for r in result["rows"]}
        result["rows"].extend(
            {"sku": sku, "quantity": None, "status": "missing_summary_sku"}
            for sku in skus
            if sku not in found
        )
        result.update(
            start=start.isoformat(),
            end=(stop - timedelta(days=1)).isoformat(),
            source_field=summary[metric],
            kind="summary",
            snapshot_date=(as_of - timedelta(days=1)).isoformat(),
        )
        return result

    def sales_window(self, source: dict, skus: list[str], start: date, stop: date) -> dict:
        """
        功能说明：读取指定日期范围的出库件数，明细去重与逐日覆盖规则保持一致。

        参数：
            source：平台来源及筛选配置；快照来源仅支持现有30天滚动指标。
            skus：待查询的商品编码集合。
            start：包含的业务起始日期。
            stop：不包含的业务结束日期。
        返回值：逐SKU数量及覆盖问题；快照任意区间不允许以滚动值代替。
        """
        if source.get("summary") and source.get("summary_metric"):
            return self.summary_window(source, skus, start, stop)
        self._validate_skus(skus)
        days_count = (stop - start).days
        if not 1 <= days_count <= 366:
            raise ValueError("查询区间需要为1至366个自然日")
        if source["kind"] == "snapshot" and days_count != 30:
            raise ValueError("滚动30天快照不能用于其他长度的历史区间")
        as_of, end = stop, stop - timedelta(days=1)
        LOG.debug("开始读取平台发货数据：platform=%s start=%s end=%s", source["id"], start, end)
        result = {
            "platform": source["id"],
            "source_table": source["table"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": [],
            "issues": [],
        }
        if not skus:
            result["issues"].append("no_requested_skus")
            return result
        fields = {key: identifier(value) for key, value in source["fields"].items()}
        table = identifier(source["table"])
        condition, params = _filters(source)
        latest = self._read(
            f"SELECT MAX({fields['date']}) AS latest FROM {table} WHERE {condition}", tuple(params)
        )
        result["latest_source_date"] = (
            str(latest[0]["latest"]) if latest and latest[0]["latest"] else None
        )
        window_start = end if source["kind"] == "snapshot" else start
        condition += f" AND {fields['date']} >= %s AND {fields['date']} < %s"
        params += [window_start.isoformat(), as_of.isoformat()]
        if source["kind"] == "detail":
            days = self._read(
                f"SELECT DATE({fields['date']}) AS day, COUNT(*) AS n FROM {table} "
                f"WHERE {condition} GROUP BY DATE({fields['date']})",
                tuple(params),
            )
            observed = {str(item["day"])[:10] for item in days}
            missing = [
                (start + timedelta(days=n)).isoformat()
                for n in range(days_count)
                if (start + timedelta(days=n)).isoformat() not in observed
            ]
            result["missing_dates"] = missing
            if missing:
                result["issues"].append("incomplete_daily_coverage")
        condition += f" AND {fields['sku']} IN ({','.join(['%s'] * len(skus))})"
        params += skus
        if source["kind"] == "snapshot":
            rows = self._read(
                f"SELECT {fields['sku']} AS sku, {fields['row_id']} AS row_id, "
                f"{fields['quantity']} AS quantity FROM {table} WHERE {condition}",
                tuple(params),
            )
            counts = Counter(str(row["sku"]) for row in rows)
            for row in rows:
                sku = str(row["sku"])
                result["rows"].append(
                    {
                        "sku": sku,
                        "quantity": units(row["quantity"]),
                        "status": "duplicate_source_sku" if counts[sku] > 1 else "matched",
                    }
                )
            if not rows:
                result["issues"].append("snapshot_or_skus_missing")
        else:
            keys = ", ".join(identifier(value) for value in source["unique_fields"])
            qty = fields["quantity"]
            missing_key = " OR ".join(
                f"{identifier(key)} IS NULL OR {identifier(key)} = ''"
                for key in source["unique_fields"]
            )
            # 同一业务明细的重复投影只计一次，数量或日期冲突时交人工核对。
            facts = (
                f"SELECT {fields['sku']} AS sku, MAX({qty}) AS quantity, COUNT(*) AS n, "
                f"SUM(CASE WHEN {qty} IS NULL OR {qty} < 0 OR {qty} != FLOOR({qty}) "
                f"THEN 1 ELSE 0 END) AS invalid_count, "
                f"CASE WHEN COUNT(DISTINCT {qty}) > 1 "
                f"OR COUNT(DISTINCT {fields['date']}) > 1 "
                f"OR SUM(CASE WHEN {missing_key} THEN 1 ELSE 0 END) > 0 "
                f"THEN 1 ELSE 0 END AS conflicts "
                f"FROM {table} WHERE {condition} GROUP BY {keys}"
            )
            rows = self._read(
                "SELECT sku, SUM(quantity) AS quantity, SUM(n) AS n, COUNT(*) AS fact_count, "
                "SUM(invalid_count) AS invalid_count, SUM(conflicts) AS conflicts "
                f"FROM ({facts}) AS facts GROUP BY sku",
                tuple(params),
            )
            for row in rows:
                status = (
                    "conflicting_source_detail"
                    if row["conflicts"]
                    else "invalid_quantity"
                    if row["invalid_count"]
                    else "matched"
                )
                result["rows"].append(
                    {
                        "sku": str(row["sku"]),
                        "quantity": units(row["quantity"]) if status == "matched" else None,
                        "status": status,
                        "source_row_count": int(row["n"]),
                        "deduplicated_rows": int(row["n"]) - int(row["fact_count"]),
                    }
                )
        LOG.debug(
            "平台发货数据读取完成：platform=%s rows=%d issues=%s",
            source["id"],
            len(result["rows"]),
            result["issues"],
        )
        return result

    @staticmethod
    def _validate_skus(skus: list[str]) -> None:
        if len(skus) > 2000 or any(not isinstance(sku, str) or not sku for sku in skus):
            raise ValueError("每次需要提供不超过 2000 个有效商品编码")


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
        LOG.debug("整款主数据读取完成：styles=%d rows=%d", len(styles), len(rows))
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
        if source.get("summary") and source.get("summary_metric"):
            return self.summary_window(source, skus, start, stop)
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
        LOG.debug("开始读取日出库：platform=%s start=%s stop=%s", source["id"], start, stop)
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
        LOG.debug("日出库读取完成：platform=%s issues=%s", source["id"], result["issues"])
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
        LOG.debug(
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
        LOG.debug("固定周期销量读取完成：platform=%s issues=%s", source["id"], result["issues"])
        return result

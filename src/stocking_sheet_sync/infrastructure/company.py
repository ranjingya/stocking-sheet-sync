from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from stocking_sheet_sync.domain.products import units
from stocking_sheet_sync.settings import identifier

LOG = logging.getLogger(__name__)


class CompanyReader:
    """读取ADS公司分组近30天出库，并合并MySQL京东自营近30天出库。"""

    def __init__(self, reader, config, jd_source):
        """reader负责只读查询，config定义ADS来源，jd_source定义京东来源。"""
        self.reader, self.config, self.jd_source = reader, config, jd_source

    def summary_window(self, skus, start, stop):
        """
        功能说明：读取基准日前一天的公司ADS近30天快照，逐SKU校验数量。

        参数：
            skus：需求表中待查询的唯一SKU集合。
            start：包含的近30天起始日。
            stop：不包含的结束日，即基准日。
        返回值：逐SKU公司分组数量及快照证据；缺失、重复或无效值留空。
        """
        self.reader._validate_skus(skus)
        if not skus or len(set(skus)) != len(skus) or (stop - start).days != 30:
            raise ValueError("全公司出库需要非空唯一SKU集合及30天区间")
        source = self.config["summary"]
        fields = {key: identifier(value) for key, value in source["fields"].items()}
        snapshot = stop - timedelta(days=1)
        LOG.info("全公司ADS读取：快照=%s，SKU数=%d", snapshot, len(skus))
        raw = self.reader._read(
            f"SELECT {fields['sku']} AS sku, {fields['quantity']} AS quantity "
            f"FROM {identifier(source['table'])} WHERE {fields['date']} >= %s "
            f"AND {fields['date']} < %s AND {fields['sku']} IN "
            f"({','.join(['%s'] * len(skus))})",
            (str(snapshot), str(stop), *skus),
        )
        grouped = defaultdict(list)
        for row in raw:
            grouped[str(row["sku"])].append(row)
        result = []
        for sku in skus:
            records = grouped[sku]
            quantity, issues = None, []
            if not records:
                issues.append("公司ADS近30天快照缺失")
            elif len(records) != 1:
                issues.append("公司ADS近30天快照重复")
            else:
                try:
                    quantity = units(records[0]["quantity"])
                except (ValueError, TypeError):
                    issues.append("公司ADS近30天数量无效")
            result.append(
                {
                    "sku": sku,
                    "quantity": quantity,
                    "issues": issues,
                    "source_table": source["table"],
                    "snapshot_date": str(snapshot),
                    "source_field": source["fields"]["quantity"],
                    "records": records,
                }
            )
        return result

    def window(self, skus, start, stop):
        """
        功能说明：按相同SKU和快照日合并ADS公司出库与京东自营近30天出库。

        参数：
            skus：需求表中待查询的唯一SKU集合。
            start：包含的近30天起始日。
            stop：不包含的结束日，即基准日。
        返回值：逐SKU总量及两部分明细；任一来源不完整时该SKU总量为空。
        """
        company_rows = {row["sku"]: row for row in self.summary_window(skus, start, stop)}
        jd_report = self.reader.daily_window(self.jd_source, skus, start, stop)
        jd = defaultdict(list)
        for row in jd_report["rows"]:
            jd[row["sku"]].append(row)
        result = []
        for sku in skus:
            company = company_rows[sku]
            candidates = jd[sku]
            valid = (
                len(candidates) == 1
                and candidates[0]["status"] == "matched"
                and candidates[0]["quantity"] is not None
            )
            issues = list(company["issues"])
            if not valid:
                issues.append("京东自营近30天出库缺失或不完整")
            jd_qty = units(candidates[0]["quantity"]) if valid else None
            result.append(
                {
                    "sku": sku,
                    "quantity": company["quantity"] + jd_qty if not issues else None,
                    "issues": issues,
                    "company": company,
                    "jd": candidates,
                    "jd_quantity": jd_qty,
                }
            )
        return result

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import timedelta
from urllib.parse import quote

from stocking_sheet_sync.domain.products import units
from stocking_sheet_sync.infrastructure.warehouse import _filters
from stocking_sheet_sync.settings import identifier

LOG = logging.getLogger(__name__)


def field_texts(value):
    """展开多维表文本、单选与查找引用字段，返回各个非空文本值。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [text for item in value for text in field_texts(item)]
    if isinstance(value, dict):
        for key in ("value", "text", "name"):
            if key in value:
                return field_texts(value[key])
    raise ValueError("店铺分组字段格式无法识别")


class CompanyReader:
    """读取飞书店铺口径、公司销售出库与京东终端出库。"""

    def __init__(self, reader, client, config, jd_source):
        """用reader查询数据库、client读取店铺、config定义公司口径、jd_source定义京东来源。"""
        self.reader, self.client, self.config = reader, client, config
        self.jd_source = jd_source
        self._groups = None

    def group_options(self):
        """沿分组字段的查找引用读取单选元数据，将选项ID映射为业务名称。"""
        cfg = self.config["shops"]
        table, field_name, field_id = cfg["table_id"], cfg["group_field"], None
        visited = set()
        while True:
            identity = (table, field_id or field_name)
            if identity in visited:
                raise ValueError("公司分组字段存在循环引用")
            visited.add(identity)
            path = (
                f"/open-apis/bitable/v1/apps/{quote(cfg['app_token'], safe='')}"
                f"/tables/{quote(table, safe='')}/fields"
            )
            fields, pages, params = [], set(), {"page_size": 100}
            while True:
                data = self.client._request("GET", path, params=params)
                fields.extend(data.get("items", []))
                if not data.get("has_more"):
                    break
                token = data.get("page_token")
                if not token or token in pages:
                    raise RuntimeError("店铺字段元数据分页不完整")
                pages.add(token)
                params = {**params, "page_token": token}
            matches = [
                f
                for f in fields
                if (f["field_id"] == field_id if field_id else f["field_name"] == field_name)
            ]
            if len(matches) != 1:
                raise ValueError("公司分组字段无法唯一定位")
            field = matches[0]
            prop = field.get("property") or {}
            if field.get("type") == 19:
                table = prop.get("filter_info", {}).get("target_table")
                field_id = prop.get("target_field")
                if not table or not field_id:
                    raise ValueError("公司分组查找引用缺少来源字段")
                continue
            return {o["id"]: o["name"] for o in prop.get("options", [])}

    def shop_groups(self):
        """分页读取配置中的店铺分类；同一店铺冲突或无分类时标记未知。"""
        if self._groups is not None:
            return self._groups
        cfg = self.config["shops"]
        options = self.group_options()
        path = (
            f"/open-apis/bitable/v1/apps/{quote(cfg['app_token'], safe='')}"
            f"/tables/{quote(cfg['table_id'], safe='')}/records"
        )
        params = {
            "page_size": 500,
            "field_names": json.dumps([cfg["id_field"], cfg["group_field"]], ensure_ascii=False),
        }
        groups, seen = defaultdict(set), set()
        while True:
            data = self.client._request("GET", path, params=params)
            for record in data.get("items", []):
                fields = record["fields"]
                ids = field_texts(fields.get(cfg["id_field"]))
                values = [
                    options.get(value, value)
                    for value in field_texts(fields.get(cfg["group_field"]))
                ]
                if len(ids) != 1:
                    raise ValueError("店铺记录缺少唯一聚水潭编码")
                groups[ids[0]].update(values or [""])
            if not data.get("has_more"):
                break
            token = data.get("page_token")
            if not token or token in seen:
                raise RuntimeError("店铺分组分页不完整")
            seen.add(token)
            params = {**params, "page_token": token}
        if not groups:
            raise ValueError("店铺分组表为空")
        self._groups = {
            key: next(iter(values)) if len(values) == 1 else "" for key, values in groups.items()
        }
        LOG.info("全公司店铺分组读取完成：店铺数=%d", len(groups))
        return self._groups

    def detail_window(self, skus, start, stop):
        """
        功能说明：按业务明细去重，并用店铺公司分组统计指定SKU的出库量。

        参数：skus：需求表SKU；start：包含的起始日；stop：不包含的结束日。
        返回值：每SKU公司分组数量及排除证据；仅明确归为公司的店铺计入。
        """
        source = self.config["detail"]
        self.reader._validate_skus(skus)
        if not skus or not 1 <= (stop - start).days <= 366:
            raise ValueError("全公司出库需要非空SKU及1至366天区间")
        groups = self.shop_groups()
        fields = {k: identifier(v) for k, v in source["fields"].items()}
        table = identifier(source["table"])
        condition, params = _filters(source)
        condition += f" AND {fields['date']} >= %s AND {fields['date']} < %s"
        params += [str(start), str(stop)]
        days = self.reader._read(
            f"SELECT DISTINCT DATE({fields['date']}) AS day FROM {table} WHERE {condition}",
            tuple(params),
        )
        observed = {str(row["day"])[:10] for row in days}
        missing = [
            str(start + timedelta(days=n))
            for n in range((stop - start).days)
            if str(start + timedelta(days=n)) not in observed
        ]
        condition += f" AND {fields['sku']} IN ({','.join(['%s'] * len(skus))})"
        params += skus
        keys = ", ".join(identifier(v) for v in source["unique_fields"])
        missing_key = " OR ".join(
            f"{identifier(v)} IS NULL OR {identifier(v)} = ''" for v in source["unique_fields"]
        )
        qty, shop = fields["quantity"], fields["shop"]
        facts = (
            f"SELECT {fields['sku']} AS sku, MAX({shop}) AS shop, MAX({qty}) AS quantity, "
            f"SUM(CASE WHEN {qty} IS NULL OR {qty}<0 OR {qty}!=FLOOR({qty}) "
            "THEN 1 ELSE 0 END) AS invalid_count, "
            f"CASE WHEN COUNT(DISTINCT {qty})>1 OR COUNT(DISTINCT {fields['date']})>1 "
            f"OR COUNT(DISTINCT {shop})>1 OR SUM(CASE WHEN {missing_key} "
            f"OR {shop} IS NULL OR {shop}='' THEN 1 ELSE 0 END)>0 "
            f"THEN 1 ELSE 0 END AS conflicts FROM {table} WHERE {condition} GROUP BY {keys}"
        )
        raw = self.reader._read(
            "SELECT sku, shop, SUM(quantity) AS quantity, SUM(invalid_count) AS invalid_count, "
            f"SUM(conflicts) AS conflicts FROM ({facts}) AS facts GROUP BY sku, shop",
            tuple(params),
        )
        result = {
            sku: {
                "sku": sku,
                "quantity": 0,
                "issues": [],
                "missing_dates": missing,
                "excluded_shops": [],
            }
            for sku in skus
        }
        included = self.config["shops"]["included_group"]
        excluded = self.config["shops"]["excluded_group"]
        for row in raw:
            item = result[str(row["sku"])]
            group = groups.get(str(row["shop"]))
            if row["conflicts"]:
                item["issues"].append("出库明细身份或店铺冲突")
            elif group != included:
                item["excluded_shops"].append(
                    {
                        "shop": str(row["shop"]),
                        "quantity": str(row["quantity"]),
                        "reason": "明确不计入" if group == excluded else "未归入公司分组",
                    }
                )
            elif row["invalid_count"]:
                item["issues"].append("公司出库数量无效")
            else:
                item["quantity"] += units(row["quantity"])
        for item in result.values():
            if missing:
                item["issues"].append("公司出库周期数据不完整")
            if item["issues"]:
                item["quantity"] = None
        return list(result.values())

    def window(self, skus, start, stop):
        """
        功能说明：合并公司分组出库和京东终端出库，单SKU异常隔离。

        参数：skus：待查SKU；start：包含的起始日；stop：不包含的结束日。
        返回值：逐SKU总量及两部分明细；任一来源不完整时该SKU总量为空。
        """
        detail = {r["sku"]: r for r in self.detail_window(skus, start, stop)}
        jd_report = self.reader.daily_window(self.jd_source, skus, start, stop)
        jd = defaultdict(list)
        for row in jd_report["rows"]:
            jd[row["sku"]].append(row)
        result = []
        for sku in skus:
            company = detail[sku]
            candidates = jd[sku]
            valid = (
                len(candidates) == 1
                and candidates[0]["status"] == "matched"
                and candidates[0]["quantity"] is not None
            )
            issues = list(company["issues"])
            if not valid:
                issues.append("京东自营后续周期出库缺失或不完整")
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

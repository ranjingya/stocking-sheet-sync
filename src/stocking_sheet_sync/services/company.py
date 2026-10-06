from __future__ import annotations

import logging
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta

from stocking_sheet_sync.domain.products import normalize_text, units
from stocking_sheet_sync.domain.sheets.company import read_company_sales
from stocking_sheet_sync.infrastructure.company import CompanyReader
from stocking_sheet_sync.settings import business_view

LOG = logging.getLogger(__name__)


def company_loader(reader, client, path, sources):
    """按path规则组装公司ADS与京东只读来源；reader读取数据，client查询周期店铺分组，sources提供京东配置。"""
    config = business_view(path, "company")
    provider = CompanyReader(reader, config, sources["daily"][config["jd_platform"]], client=client)
    return provider.window


def resolve_company_sales(snapshot, rows, catalog, as_of, rules, load_window, *, overwrite=False):
    """
    功能说明：读取与基准日一致的全公司近30天销量，缺失时查询并生成补充计划。

    参数：
        snapshot：原始表格快照。
        rows：已核实身份的需求商品行。
        catalog：用于核对款式身份的商品主数据。
        as_of：本次基准日。
        rules：公司销量列标题和日期规则。
        load_window：接收SKU、包含起日、不含止日的只读查询函数。
        overwrite：是否允许替换自动生成列中的已有数量。
    返回值：逐SKU数量、目标标题和原因；不会修改表格或覆盖人工公司列。
    """
    existing = read_company_sales(snapshot, rows, rules, as_of=as_of)
    windows, errors = {}, {}
    for style in sorted({r["style"] for r in rows}):
        records = [record for record in catalog if record["style"] == style]
        try:
            if not records or any(r.get("issues") for r in rows if r["style"] == style):
                raise ValueError("商品身份未确认")
            windows[style] = (as_of - timedelta(days=30), as_of)
        except ValueError as error:
            errors[style] = str(error)
    prefix = rules["company_sheet"]["generated_prefix"]
    periods = {
        f"{start:%y}.{start.month}.{start.day}-{end:%y}.{end.month}.{end.day}"
        for start, stop in windows.values()
        for end in [stop - timedelta(days=1)]
    }
    header = prefix + "、".join(sorted(periods))
    generated = [c for c in existing["candidates"] if c.get("generated")]
    if len(generated) > 1:
        return {
            **existing,
            "status": "unavailable",
            "rows": [],
            "reasons": ["全公司：存在多个自动出库列，无法确定目标列"],
        }
    old = generated[0] if generated else None
    if old and normalize_text(old["period"]) != normalize_text(header) and not overwrite:
        return {
            **existing,
            "status": "unavailable",
            "rows": [],
            "reasons": ["全公司：已有出库列日期与本次近30天不一致，未覆盖"],
        }
    if not periods:
        return {"status": "unavailable", "rows": [], "reasons": ["全公司：无法确定近30天查询范围"]}
    if (
        old
        and existing.get("generated")
        and existing["status"] == "available"
        and normalize_text(old["period"]) == normalize_text(header)
        and not overwrite
        and all(r["quantity"] is not None for r in existing["rows"])
    ):
        return existing
    result = {
        "automatic": True,
        "header": header,
        "rows": [],
        "reasons": [],
        "windows": {
            style: {"start": str(start), "stop": str(stop)}
            for style, (start, stop) in windows.items()
        },
    }
    by_window = defaultdict(list)
    for row in rows:
        if row["style"] in windows:
            by_window[windows[row["style"]]].append(row)
        else:
            result["rows"].append(
                {
                    "sku": row["sku"],
                    "row": row["row"],
                    "style": row["style"],
                    "quantity": None,
                    "issues": [errors.get(row["style"], "无法确定周期")],
                }
            )
    # 同列不同周期不得遗留旧数值；强制刷新也只有全部数据可用才允许换标题。
    changing_period = bool(old and normalize_text(old["period"]) != normalize_text(header))
    for (start, stop), requested in by_window.items():
        skus = sorted({r["sku"] for r in requested})
        LOG.info(
            "全公司出库查询：SKU数=%d，周期=%s至%s", len(skus), start, stop - timedelta(days=1)
        )
        try:
            raw = load_window(skus, start, stop)
            values = defaultdict(list)
            for item in raw:
                values[item["sku"]].append(item)
        except Exception as error:
            LOG.warning("全公司出库查询失败：%s", error)
            values = {}
            result["reasons"].append(f"全公司：来源读取失败（{error}）")
        for row in requested:
            items = values.get(row["sku"], [])
            item = (
                deepcopy(items[0])
                if len(items) == 1
                else {"sku": row["sku"], "quantity": None, "issues": ["出库数据缺失或不唯一"]}
            )
            item.update(row=row["row"], style=row["style"])
            item.setdefault("issues", [])
            if item.get("quantity") is not None:
                try:
                    item["quantity"] = units(item["quantity"])
                except ValueError:
                    item["quantity"] = None
                    item["issues"].append("出库数量无效")
            if item["issues"]:
                item["quantity"] = None
            if old:
                cell = snapshot["cells"].get(f"{old['column']}{row['row']}", {})
                value = cell.get("value")
                if cell.get("formula") or (
                    value not in (None, "")
                    and (type(value) not in (int, float) or value != item["quantity"])
                    and not overwrite
                ):
                    item["quantity"] = None
                    item["issues"].append("表内全公司出库与查询结果不同，未覆盖")
            result["rows"].append(item)
    # 全公司出库按款完整填写，任一需求SKU缺失时整款不参与写入和占比分配。
    incomplete_styles = {r["style"] for r in result["rows"] if r["quantity"] is None}
    for item in result["rows"]:
        if item["style"] in incomplete_styles:
            item["quantity"] = None
            reason = f"{item['style']}：全公司出库数据不完整，整款未填充"
            if reason not in item["issues"]:
                item["issues"].append(reason)
    bad = [r for r in result["rows"] if r["quantity"] is None]
    if changing_period and bad:
        result.update(automatic=False, rows=[], status="unavailable")
        result["reasons"].append("全公司：新周期数据不完整，保留原周期列")
        return result
    result["status"] = "partial" if bad else "available"
    for reason in dict.fromkeys(issue for r in bad for issue in r.get("issues", [])):
        result["reasons"].append("全公司：" + reason)
    if not old and not any(r["quantity"] is not None for r in result["rows"]):
        result.update(automatic=False, status="unavailable")
        result["reasons"].append("全公司：所有款数据均不完整，不新增出库列")
    LOG.log(
        logging.WARNING if bad else logging.INFO,
        "全公司出库查询结束：可用%d/%d个SKU%s",
        len(result["rows"]) - len(bad),
        len(result["rows"]),
        "；" + "；".join(result["reasons"]) if bad else "",
    )
    return result

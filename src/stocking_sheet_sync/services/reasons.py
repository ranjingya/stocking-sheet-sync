"""通知与日志共用的业务原因说明。"""

WINDOW_NAMES = {
    "current": "近30天",
    "previous": "去年同期近30天",
    "historical_future": "去年后续周期",
}

ISSUE_NAMES = {
    "previous_sales_base_low": "去年同期30天基数过低",
    "previous_sales_zero": "去年同期销量为0",
    "company_recent_sales_unavailable": "缺少可用的全公司近30天销量",
    "占比参考销量为零，无法分配需求": "近30天销量合计为0，无法计算SKU占比",
    "missing_rolling_snapshot": "销量快照缺失",
    "duplicate_source_sku_day": "同一SKU同一天存在重复记录",
    "invalid_rolling_value": "快照数量无效",
    "invalid_daily_value": "每日出库数量无效",
    "incomplete_sku_daily_coverage": "SKU每日数据不完整",
    "incomplete_daily_coverage": "每日数据不完整",
    "snapshot_or_skus_missing": "销量快照或所需SKU缺失",
    "rolling_source_needs_review": "近30天销量快照不可用",
    "daily_source_needs_review": "每日出库数据未通过校验",
    "source_read_failed": "销量来源读取失败",
    "quantity_missing": "缺少可用数量",
    "needs_review": "数据需要核对",
    "read_failed": "销量来源读取失败",
    "product_identity_unresolved": "商品身份不明确",
    "catalog_sku_missing": "商品主数据中没有该SKU",
    "catalog_sku_ambiguous": "SKU对应多条商品主数据",
    "missing_sku": "缺少SKU",
    "duplicate_sheet_sku": "表内SKU重复",
    "style_mismatch": "款号与主数据不一致",
    "name_mismatch": "商品名称与主数据不一致",
    "spec_mismatch": "规格与主数据不一致",
    "style_missing": "缺少款号",
    "name_missing": "缺少商品名称",
    "spec_missing": "缺少规格",
    "no_product_rows": "未找到商品行",
    "style_set_mismatch": "表内款号与计算结果不一致",
    "catalog_or_season_unresolved": "商品主数据或季节信息无法确定",
    "platform_results_incomplete": "平台计算结果不完整",
    "quantity_unavailable": "缺少可用数量",
    "target_conflict": "目标单元格已有不同内容",
}


def describe_issues(issues: list[str]) -> str:
    """将 issues 中的问题转换为中文并去重，返回简短说明。"""
    texts = []
    for issue in issues:
        text = str(issue)
        for code, label in ISSUE_NAMES.items():
            text = text.replace(code, label)
        for key, label in WINDOW_NAMES.items():
            text = text.replace(key + ":", label + "：")
        texts.append(text)
    return "；".join(dict.fromkeys(texts)) or "执行结果未确认"


def platform_reason(item: dict, *, history_only: bool = False) -> str:
    """
    功能说明：提取平台失败原因，优先展示具体周期、日期及SKU证据。

    参数：
        item：平台计算结果，包含输入、来源与问题。
        history_only：仅提取历史来源问题，排除预测计算问题。
    返回值：适合通知与日志的中文原因。
    """
    reasons = []
    for key, label in WINDOW_NAMES.items():
        if key in item.get("inputs", {}):
            continue
        source = item.get("sources", {}).get(key, {})
        details = []
        for row in source.get("rows", []):
            if row.get("issues"):
                details.append(f"SKU {row['sku']}：{describe_issues(row['issues'])}")
        if not details:
            details = [describe_issues(source["issues"])] if source.get("issues") else []
        if details:
            period = source.get("snapshot_date") or "~".join(
                str(source.get(k, "")) for k in ("start", "end")
            ).strip("~")
            reasons.append(
                f"{label}（{period}）：" + "；".join(details)
                if period
                else f"{label}：" + "；".join(details)
            )
    if reasons and any("缺少日期证据" in issue for issue in item.get("issues", [])):
        reasons.append("表内历史值缺少日期证据")
    if reasons:
        return "；".join(dict.fromkeys(reasons))
    issues = item.get("issues", [])
    if history_only:
        issues = [issue for issue in issues if any(issue.startswith(k + ":") for k in WINDOW_NAMES)]
    return describe_issues(issues)


def target_reason(issues: list[dict]) -> str:
    """将目标检查 issues 转为含行号、SKU及具体问题的通知说明。"""
    reasons = []
    for issue in issues:
        location = "，".join(
            f"{label}{issue[key]}"
            for key, label in (("row", "第"), ("sku", "SKU "), ("style", "款号 "))
            if issue.get(key) is not None
        )
        if issue.get("row") is not None:
            location = location.replace(f"第{issue['row']}", f"第{issue['row']}行", 1)
        detail = describe_issues(issue.get("details") or [issue["reason"]])
        reasons.append(f"{location}：{detail}" if location else detail)
    return "；".join(dict.fromkeys(reasons))

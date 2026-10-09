"""通知原因的展示摘要；完整证据由原始报告保留。"""

import re
from collections import OrderedDict


def _brief(text: str) -> str:
    """将单条原因归类为业务说明，避免展开SKU、日期和底层异常。"""
    if "ADS 表周期无数据" in text and "DWD" in text:
        return "ADS 表周期无数据，使用 DWD 明细表填充"
    if "所有款数据均不完整" in text:
        return "所有款数据不完整，未新增列"
    if "整款未填充" in text:
        return "全公司出库数据不完整，整款未填充"
    if "京东出库缺失" in text:
        return "京东出库缺失"
    if "京东自营近30天出库缺失或不完整" in text:
        return "京东自营近30天数据不完整"
    if "全公司近30天销量" in text:
        return "缺少全公司近30天销量，未预测"
    if "去年同期30天基数过低" in text:
        return "去年同期30天基数过低，未预测"
    if "历史数据不一致" in text or "历史单元格已有" in text or "目标单元格已有" in text:
        return "历史数据不一致，相关款平台已跳过"
    if "预测合计单元格" in text:
        return "预测合计存在冲突，未更新"
    if "已有不同预测" in text:
        return "已有预测保留"
    if any(word in text for word in ("工作表", "缺少表头")):
        return "无法确定符合要求的下单工作表"
    if any(word in text for word in ("商品身份", "主数据", "款号与", "规格与", "表内SKU重复")):
        return "商品信息匹配异常，相关数据未填充"
    if any(
        word in text
        for word in (
            "无权限",
            "没有访问权限",
            "权限不足",
            "错误码 1044",
            "错误码 1045",
            "错误码 1142",
        )
    ):
        return "没有访问权限"
    if "错误码 1064" in text:
        return "数据库查询失败，相关数据未填充"
    if "读取失败" in text:
        return "数据读取失败"
    if any(word in text for word in ("Traceback", "SQL", "Connection", "HTTP", "Timeout")):
        return "服务请求失败，详情见日志"
    if any(
        word in text
        for word in (
            "快照",
            "SKU销量缺失或不唯一",
            "quantity_unavailable",
            "每日数据",
            "每日出库",
            "来源校验",
            "缺少可用数量",
            "出库缺失",
            "数量无效",
            "重复记录",
        )
    ):
        periods = []
        # 去年同期与今年窗口分别识别，避免将同期近30天误认为今年。
        remaining = text.replace("去年同期近30天", "去年同期30天")
        if "近30天" in remaining:
            periods.append("近30天")
        if "去年同期" in text:
            periods.append("去年同期")
        if "后续周期" in text:
            periods.append("后续周期")
        prefix = "、".join(periods) or "来源"
        impact = "，未预测" if "未计算预测" in text else "，相关数据未填充"
        return prefix + "数据缺失或不完整" + impact
    if "缺少日期证据" in text or "日期与" in text:
        return "历史日期无法确认，相关数据未填充"
    # 未归类的短业务说明原样保留；长文本不截取错误堆栈冒充业务结论。
    return text if len(text) <= 80 else "处理异常，详细原因见日志及核对报告"


def compact_reasons(reasons: list[str]) -> list[str]:
    """
    功能说明：按平台和同类原因合并通知，限制通知长度并保留款式范围。

    参数：
        reasons：历史、预测和任务级的完整原因文本列表，不修改原内容。
    返回值：每个平台一行的简短说明；详细证据仍保留在调用方报告中。
    """
    grouped = OrderedDict()
    for reason in reasons:
        for line in reason.splitlines():
            line = re.sub(r"\s+", " ", line.replace("**", "")).strip()
            if not line:
                continue
            match = re.match(r"^([^：:（）]{1,24})(?:（([^）]+)）)?[：:](.*)$", line)
            label, style, body = match.groups() if match else ("", None, line)
            if re.search(r"[A-Za-z_]{4,}|SKU|第\d+行", label) and not label.endswith("平台"):
                # 技术异常、行号及条码不作为平台标题。
                label, style, body = "", None, line
            if label == "全公司":
                company_style = re.match(r"^([^：]{1,32})：全公司出库数据不完整", body)
                if company_style:
                    style = company_style[1]
            brief = _brief(body)
            bucket = grouped.setdefault(label, OrderedDict())
            scope = bucket.setdefault(brief, {"styles": set(), "unscoped": False})
            if style:
                scope["styles"].add(style)
            else:
                scope["unscoped"] = True
    lines = []
    for label, bucket in grouped.items():
        # 相同款式范围的多个历史窗口与预测失败合并，避免一项问题重复展示。
        merged = OrderedDict()
        for brief, scope in list(bucket.items()):
            match = re.fullmatch(r"(.+)数据缺失或不完整，(相关数据未填充|未预测)", brief)
            if not match:
                continue
            key = (tuple(sorted(scope["styles"])), scope["unscoped"])
            group = merged.setdefault(
                key, {"periods": [], "impacts": [], "scope": scope, "briefs": []}
            )
            group["periods"].extend(match[1].split("、"))
            group["impacts"].append(match[2])
            group["briefs"].append(brief)
        for group in merged.values():
            if len(group["briefs"]) < 2:
                continue
            for brief in group["briefs"]:
                bucket.pop(brief)
            periods = "、".join(dict.fromkeys(group["periods"]))
            impacts = set(group["impacts"])
            effect = "相关历史未填充，未预测" if len(impacts) > 1 else group["impacts"][0]
            bucket[periods + "数据缺失或不完整，" + effect] = group["scope"]
        if label == "全公司" and "全公司出库数据不完整，整款未填充" in bucket:
            bucket = OrderedDict(
                (brief, scope) for brief, scope in bucket.items() if "数据缺失或不完整" not in brief
            )
        parts = []
        for brief, scope in bucket.items():
            styles = sorted(scope["styles"])
            suffix = ""
            if styles and not scope["unscoped"]:
                names = "、".join(styles)
                suffix = (
                    f"（{names}）"
                    if len(styles) <= 2 and len(names) <= 32
                    else f"（涉及{len(styles)}款）"
                )
            parts.append(brief + suffix)
        # 平台同时出现很多不同错误时，保留提示并将细节交给报告。
        text = "；".join(parts)
        if len(text) > 180:
            text = "；".join(parts[:2]) + "；另有其他问题，详情见核对报告"
        lines.append((label + "：" if label else "") + text)
    if len(lines) > 8:
        lines = lines[:8] + ["另有其他问题，详情见日志及核对报告"]
    return lines

from __future__ import annotations

import html
import logging
import re
from typing import Any, Literal


def summarize_forecast(report: dict, config: dict, blocked: dict | None = None) -> dict:
    """
    功能说明：汇总核验完成的各平台预测结果，保存完整平台数和人工判断原因。

    参数：
        report：已完成写入核验的预测报告。
        config：包含平台ID和名称的配置。
        blocked：本次未写入的平台及简短原因。
    返回值：可持久化的通知摘要，不依赖本地报告文件。
    """
    reasons = []
    complete = 0
    any_ready = False
    reasons_map = {
        "previous_sales_zero": "去年同期销量为0",
        "company_lifecycle_sales_unavailable": "缺少可用的全公司生命周期销量",
    }
    platforms = config["platforms"]
    for platform in platforms:
        if platform["id"] in (blocked or {}):
            reasons.append(blocked[platform["id"]])
            continue
        items = [
            (group["style"], item)
            for group in report["groups"]
            for item in group["platforms"]
            if item["platform"] == platform["id"]
        ]
        ready = sum(item["status"] == "ready" for _, item in items)
        any_ready |= bool(ready)
        if items and ready == len(items):
            complete += 1
        for style, item in items:
            if item["status"] != "ready":
                label = platform.get("name", platform["id"])
                if len(report["groups"]) > 1:
                    label += f"（{style}）"
                reason = (
                    "、".join(reasons_map.get(x, "数据需人工核对") for x in item.get("issues", []))
                    or "数据需人工核对"
                )
                reasons.append(f"{label}：{reason}，本次未计算预测（已有值不覆盖）")
    return {
        "forecast": {
            "status": "completed"
            if complete == len(platforms) and platforms
            else "partial"
            if any_ready
            else "manual",
            "completed": complete,
            "total": len(platforms),
            "reasons": list(dict.fromkeys(reasons)),
        }
    }


def summarize_platform_fill(
    update: dict,
    config: dict,
    *,
    history: bool,
    report: dict | None = None,
    history_report: dict | None = None,
) -> dict:
    """
    功能说明：按实际已核验的平台生成历史和预测通知摘要。

    参数：
        update：完成写入回读的计划，含跳过平台及原因。
        config：平台名称配置。
        history：是否启用历史填充。
        report：预测报告；仅填历史时为空。
        history_report：仅填历史时的销量来源报告。
    返回值：可保存到批次的通知摘要。
    """
    blocked = {}
    skipped_entries = update.get("skipped_entries", [])
    for platform in config["platforms"]:
        issues = update.get("blocked_platforms", {}).get(platform["id"])
        if issues is None:
            continue
        codes = " ".join(issues)
        mismatches = [
            entry
            for entry in skipped_entries
            if entry["platform"].startswith(platform["id"] + ":")
            and entry.get("metric") in {"sales", "previous", "future"}
            and "target_conflict" in entry.get("issues", [])
            and entry.get("quantity") is not None
        ]
        if mismatches:
            first = mismatches[0]
            reason = (
                f"历史数据不一致（{first['target_cell']}：表内{first['existing_quantity']}，"
                f"数仓{first['quantity']}）"
            )
            if len(mismatches) > 1:
                reason += f"，共{len(mismatches)}处"
        elif "target_conflict" in codes:
            reason = "目标历史单元格已有不同内容"
        elif "rolling" in codes or "snapshot" in codes:
            reason = "近30天销量快照不可用"
        elif "read_failed" in codes:
            reason = "销量来源读取失败"
        else:
            reason = "历史销量数据缺失或未通过校验"
        blocked[platform["id"]] = f"{platform['name']}：{reason}，该平台未填充"
    forecast_blocked = dict(blocked)
    for item in update.get("skipped_forecasts", []):
        if (
            "forecast_target_conflict" in item.get("reason", [])
            and item["platform"] not in forecast_blocked
        ):
            forecast_blocked[item["platform"]] = (
                f"{next(p['name'] for p in config['platforms'] if p['id'] == item['platform'])}："
                f"{item['target_cell']}已有不同预测，保留原值"
            )
    details = summarize_forecast(report, config, forecast_blocked) if report is not None else {}
    source_results = (
        history_report["sources"]
        if history_report is not None
        else [
            source
            for group in report["groups"]
            for item in group["platforms"]
            for source in item["sources"].values()
        ]
        if report is not None
        else []
    )
    fallback_platforms = {
        source["platform"]
        for source in source_results
        if source.get("fallback", {}).get("reason") == "ads_snapshot_day_empty"
        and not source.get("issues")
        and source["platform"] not in blocked
    }
    fallback_reasons = [
        f"{platform['name']}：ADS 表周期无数据，使用 DWD 明细表填充"
        for platform in config["platforms"]
        if platform["id"] in fallback_platforms
    ]
    if blocked:
        details["blocked_platforms"] = list(blocked)
        logging.getLogger(__name__).warning("部分平台未填充：%s", "；".join(blocked.values()))
    if history:
        total = len(config["platforms"])
        done = total - len(blocked)
        details["history"] = {
            "status": "completed" if done == total else "partial" if done else "manual",
            "completed": done,
            "total": total,
            "reasons": [*blocked.values(), *fallback_reasons],
        }
    elif "forecast" in details:
        details["forecast"]["reasons"].extend(fallback_reasons)
    return details


def build_sync_card(
    *,
    original_name: str,
    record_url: str,
    status: Literal["success", "failure"],
    target_folder_token: str = "",
    target_name: str = "",
    target_url: str = "",
    reason: str = "",
    history_status: str = "unknown",
    forecast_status: str = "unknown",
    details: dict | None = None,
    degraded: bool = False,
    original_backup_url: str = "",
    filled_backup_url: str = "",
) -> dict[str, Any]:
    """
    功能说明：生成紧凑通知，保留原始记录、实际填充状态、原因和结果按钮。

    参数：
        original_name：原记录名称。
        record_url：原记录链接。
        status：搬运成功或失败。
        target_folder_token：调用方提供的目录上下文，不在卡片显示。
        target_name：调用方提供的交付名称，不在卡片重复显示。
        target_url：已确认的结果链接。
        reason：失败或降级原因。
        history_status：历史阶段状态，unknown表示未核验。
        forecast_status：预测阶段状态，unknown表示未核验。
        details：已持久化的平台汇总。
        degraded：是否交付未填充原表。
        original_backup_url：原备份上下文，不在卡片显示。
        filled_backup_url：处理备份上下文，不在卡片显示。
    返回值：飞书Card 2.0消息体。
    """
    if status not in {"success", "failure"}:
        raise ValueError("status 必须填写 success 或 failure")
    name = _escape_markdown(_clean_text(original_name))
    if not name:
        raise ValueError("原文档名称不能为空")
    record_url = _validate_url(record_url, "原始记录链接")
    target_url = _validate_url(target_url, "结果表格链接") if target_url else ""
    if status == "success" and not target_url:
        raise ValueError("搬运成功时结果链接不能为空")
    if status == "failure" and not reason:
        raise ValueError("搬运失败时原因不能为空")
    details = details or {}
    reasons = []
    states = []
    texts = []
    for key, label, stage, done in (
        ("history", "历史数据", history_status, "已填充"),
        ("forecast", "预测", forecast_status, "已计算"),
    ):
        info = details.get(key, {}) if not degraded and status == "success" else {}
        state = info.get("status", stage)
        if degraded and state not in {"disabled", "unsupported", "skipped"}:
            state = "needs_review"
        states.append(state)
        if state == "completed":
            text = "✅ " + done
        elif state == "partial":
            text = f"⚠️ 部分完成（{info['completed']}/{info['total']}平台全部完成）"
        elif state == "disabled":
            text = "未开启"
            reasons.append(label + "开关关闭")
        elif state in {"skipped", "unsupported"}:
            text = "暂不支持" if key == "forecast" else "未填充"
            reasons.append("新品预测暂不支持" if key == "forecast" else "当前款式未开启历史填充")
        elif state == "unknown":
            text = "未核验"
            reasons.append(label + "未提供执行结果")
        else:
            text = "未填充" if key == "history" else "未计算"
            if not info.get("reasons") and not reason:
                reasons.append(label + "执行结果待核验")
        reasons.extend(info.get("reasons", []))
        texts.append(f"**{label}：**{text}")
    if reason:
        reasons.insert(0, reason)
    if degraded:
        reasons.append("已交付未填充原表")
    if status == "failure":
        title, color = "搬运失败", "red"
    elif degraded:
        title, color = "仅搬运完成", "orange"
    elif "partial" in states or ("completed" in states and "manual" in states):
        title, color = "部分完成", "orange"
    elif any(s in {"manual", "needs_review", "retryable", "running"} for s in states):
        title, color = "待人工处理", "orange"
    elif "completed" in states:
        title, color = "处理完成", "green"
    else:
        title, color = "搬运完成", "green"
    content = f"**原始记录：**[{name}]({record_url})\n" + "\n".join(texts)
    title = "下单需求 · " + title
    card = {
        "schema": "2.0",
        "config": {"width_mode": "default", "summary": {"content": title}},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "body": {
            "direction": "vertical",
            "vertical_spacing": "medium",
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _build_button(
                                    "查看结果表格" if target_url else "查看原始记录",
                                    target_url or record_url,
                                    "primary",
                                )
                            ],
                        }
                    ],
                },
            ],
        },
    }

    if reasons:
        reason_text = "\n".join(
            _escape_markdown(_clean_text(item)) for item in dict.fromkeys(reasons)
        )
        card["body"]["elements"].insert(
            1,
            {
                "tag": "markdown",
                "content": f"<font color='grey'>原因：{reason_text}</font>",
                "text_size": "notation",
            },
        )
    return card


def _build_button(text: str, url: str, button_type: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "size": "medium",
        "width": "default",
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _escape_markdown(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\[\]()])", r"\\\1", escaped)


def _validate_url(value: str, field_name: str) -> str:
    url = str(value or "").strip()
    if not re.fullmatch(r"https://\S+", url, flags=re.IGNORECASE):
        raise ValueError(f"{field_name}格式无效")
    return url

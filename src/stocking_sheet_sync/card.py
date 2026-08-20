from __future__ import annotations

import html
import re
from typing import Any, Literal


def build_sync_card(
    *,
    original_name: str,
    record_url: str,
    status: Literal["success", "failure"],
    target_name: str = "",
    target_url: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """
    功能说明：生成统一样式的产品下单同步结果卡片。

    参数：
        original_name：原文档名称。
        record_url：原多维表记录链接。
        status：同步状态，支持 success 或 failure。
        target_name：同步后表格名称，成功时必填。
        target_url：同步后表格链接，成功时必填。
        reason：失败原因，失败时必填。

    返回值：
        可直接发送为 interactive 消息的 Card 2.0 对象。
    """
    success = status == "success"
    if status not in {"success", "failure"}:
        raise ValueError("status 必须填写 success 或 failure")

    original_name = _escape_markdown(_clean_text(original_name))
    record_url = _validate_url(record_url, "原始记录链接")
    target_name = _escape_markdown(_clean_text(target_name))
    target_url = _validate_url(target_url, "同步表格链接") if target_url else ""
    reason = _escape_markdown(_clean_text(reason))

    if not original_name:
        raise ValueError("原文档名称不能为空")
    if success and (not target_name or not target_url):
        raise ValueError("同步成功时目标表格名称和链接不能为空")
    if not success and not reason:
        raise ValueError("同步失败时失败原因不能为空")

    result_text = "同步成功" if success else "同步失败"
    if success:
        detail_content = (
            f"原始记录： [{original_name}]({record_url})\n"
            f"同步表格： [{target_name}]({target_url})"
        )
        action_text = "查看同步表格"
        action_url = target_url
    else:
        detail_content = (
            f"原始记录： [{original_name}]({record_url})\n"
            "同步表格： 未生成\n"
            f"失败原因： <font color='red'>{reason}</font>"
        )
        action_text = "查看原始记录"
        action_url = record_url

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": f"产品下单同步 · {result_text}"},
        },
        "header": {
            "title": {"tag": "plain_text", "content": f"产品下单同步 · {result_text}"},
            "template": "green" if success else "red",
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "large",
            "elements": [
                {"tag": "markdown", "content": detail_content},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": action_text},
                    "type": "primary" if success else "danger",
                    "size": "medium",
                    "width": "default",
                    "behaviors": [{"type": "open_url", "default_url": action_url}],
                },
            ],
        },
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

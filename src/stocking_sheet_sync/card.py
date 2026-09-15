from __future__ import annotations

import html
import re
from typing import Any, Literal
from urllib.parse import urlsplit


def build_sync_card(
    *,
    original_name: str,
    record_url: str,
    status: Literal["success", "failure"],
    target_folder_token: str,
    target_name: str = "",
    target_url: str = "",
    reason: str = "",
    fill_summary: str = "",
    original_backup_url: str = "",
    filled_backup_url: str = "",
) -> dict[str, Any]:
    """
    功能说明：生成统一样式的产品下单同步结果卡片。

    参数：
        original_name：原文档名称。
        record_url：原多维表记录链接。
        status：搬运状态，支持 success 或 failure。
        target_folder_token：目标共享文件夹 token。
        target_name：同步后表格名称，成功时必填。
        target_url：同步后表格链接，成功时必填。
        reason：失败或填充降级原因，失败时必填。
        fill_summary：可选的历史与预测填充阶段说明。
        original_backup_url：可选原始备份链接。
        filled_backup_url：可选处理备份链接，具体填充结果由阶段状态说明。

    返回值：
        可直接发送为 interactive 消息的 Card 2.0 对象。
    """
    success = status == "success"
    if status not in {"success", "failure"}:
        raise ValueError("status 必须填写 success 或 failure")

    original_name = _escape_markdown(_clean_text(original_name))
    record_url = _validate_url(record_url, "原始记录链接")
    folder_url = _build_folder_url(record_url, target_folder_token)
    target_name = _escape_markdown(_clean_text(target_name))
    target_url = _validate_url(target_url, "同步表格链接") if target_url else ""
    reason = _escape_markdown(_clean_text(reason))

    if not original_name:
        raise ValueError("原文档名称不能为空")
    if success and (not target_name or not target_url):
        raise ValueError("同步成功时目标表格名称和链接不能为空")
    if not success and not reason:
        raise ValueError("同步失败时失败原因不能为空")

    result_text = f"搬运{'成功' if success else '失败'}"
    if not success and target_url:
        result_text = "搬运成功，填充待处理"
    if success and fill_summary and reason:
        result_text = "搬运成功，填充未完成"
    if success:
        detail_content = (
            f"原始记录： [{original_name}]({record_url})\n同步副本： [{target_name}]({target_url})"
        )
        if fill_summary and reason:
            detail_content += f"\n处理说明： {reason}"
        action_text = "查看同步副本"
        action_url = target_url
    elif target_url:
        detail_content = (
            f"原始记录： [{original_name}]({record_url})\n"
            f"同步副本： [{target_name}]({target_url})\n"
            f"处理说明： {reason}"
        )
        action_text, action_url = "查看同步副本", target_url
    else:
        detail_content = (
            f"原始记录： [{original_name}]({record_url})\n"
            "同步副本： 请核对目标文件夹\n"
            f"失败原因： <font color='red'>{reason}</font>"
        )
        action_text = "查看原始记录"
        action_url = record_url

    for label, url in (("原始备份", original_backup_url), ("处理备份", filled_backup_url)):
        if url:
            detail_content += f"\n{label}： [{label}]({_validate_url(url, label)})"

    if fill_summary:
        detail_content += "\n" + _escape_markdown(_clean_text(fill_summary))

    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "default",
            "summary": {"content": f"产品下单同步 · {result_text}"},
        },
        "header": {
            "title": {"tag": "plain_text", "content": f"产品下单同步 · {result_text}"},
            "template": ("green" if success else "red"),
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "large",
            "elements": [
                {"tag": "markdown", "content": detail_content},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [
                                _build_button(
                                    action_text,
                                    action_url,
                                    "primary" if success else "danger",
                                )
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [_build_button("打开目标文件夹", folder_url, "default")],
                        },
                    ],
                },
            ],
        },
    }


def _build_button(text: str, url: str, button_type: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": button_type,
        "size": "medium",
        "width": "default",
        "behaviors": [{"type": "open_url", "default_url": url}],
    }


def _build_folder_url(reference_url: str, folder_token: str) -> str:
    token = _clean_text(folder_token)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("目标文件夹 token 格式无效")
    parsed = urlsplit(reference_url)
    return f"{parsed.scheme}://{parsed.netloc}/drive/folder/{token}"


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

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from stocking_sheet_sync.infrastructure.feishu.client import FeishuClient
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.notification import build_sync_card
from stocking_sheet_sync.settings import load_config


class CardMessageClient(Protocol):
    def send_card(self, open_id: str, card: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ManualNotificationSummary:
    sent: int
    failed: int


def send_manual_notification(
    message_client: CardMessageClient,
    open_ids: tuple[str, ...],
    *,
    original_name: str,
    original_url: str,
    target_name: str,
    target_url: str,
    target_folder_token: str,
    logger: logging.Logger | None = None,
) -> ManualNotificationSummary:
    """
    功能说明：按手动输入生成同步结果卡片，并向所有配置接收人发送。

    参数：
        message_client：用于发送飞书卡片的消息客户端。
        open_ids：需要接收通知的用户 Open ID 列表。
        original_name：原始记录显示名称。
        original_url：原始记录链接。
        target_name：目标表格显示名称。
        target_url：目标表格链接。
        target_folder_token：目标共享文件夹 token。
        logger：可选日志记录器。

    返回值：
        成功和失败的接收人数量。
    """
    if not open_ids:
        raise ValueError("notifications.open_ids 不能为空")

    active_logger = logger or logging.getLogger(__name__)
    card = build_sync_card(
        original_name=original_name,
        record_url=original_url,
        target_name=target_name,
        target_url=target_url,
        status="success",
        target_folder_token=target_folder_token,
    )
    sent = 0
    failed = 0
    active_logger.info(
        "开始发送手动同步通知：recipients=%d target_name=%s",
        len(open_ids),
        target_name,
    )
    for open_id in dict.fromkeys(open_ids):
        try:
            message_client.send_card(open_id, card)
            sent += 1
            active_logger.info("手动同步通知发送成功：open_id=%s", open_id)
        except Exception as error:
            failed += 1
            active_logger.error(
                "手动同步通知发送失败：open_id=%s reason=%s",
                open_id,
                error,
            )
            active_logger.debug(
                "手动同步通知发送失败堆栈：open_id=%s",
                open_id,
                exc_info=True,
            )
    active_logger.info("手动同步通知发送完成：sent=%d failed=%d", sent, failed)
    return ManualNotificationSummary(sent=sent, failed=failed)


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：解析命令行参数，并执行一次不访问 Redis 的手动通知。

    参数：
        argv：可选命令行参数列表；未传入时读取当前进程参数。

    返回值：
        全部发送成功返回 0，存在失败接收人返回 1。
    """
    parser = argparse.ArgumentParser(description="手动发送产品下单同步结果卡片")
    parser.add_argument("--original-name", required=True, help="原始记录显示名称")
    parser.add_argument("--original-url", required=True, help="原始记录链接")
    parser.add_argument("--target-name", required=True, help="目标表格显示名称")
    parser.add_argument("--target-url", required=True, help="目标表格链接")
    args = parser.parse_args(argv)

    config = load_config()
    configure_logging(config.log_level)
    logger = logging.getLogger("stocking_sheet_sync.entrypoints.notify")
    message_client = FeishuClient(
        config,
        config.feishu_message_app_id,
        config.feishu_message_app_secret,
        "message",
        logger,
    )
    try:
        summary = send_manual_notification(
            message_client,
            config.notify_open_ids,
            original_name=args.original_name,
            original_url=args.original_url,
            target_name=args.target_name,
            target_url=args.target_url,
            target_folder_token=config.target_folder_token,
            logger=logger,
        )
        return int(summary.failed > 0)
    finally:
        message_client.close()


def main() -> None:
    raise SystemExit(run())

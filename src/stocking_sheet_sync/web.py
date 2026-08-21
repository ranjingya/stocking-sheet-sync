from __future__ import annotations

import atexit
import hmac
import logging
import re
from dataclasses import asdict
from typing import Protocol

from flask import Flask, jsonify, request

from .config import AppConfig, load_config
from .lark_client import FeishuClient
from .logging_config import configure_logging
from .models import SyncSummary
from .redis_store import RedisStateStore
from .sync_service import SyncBusyError, SyncService


class WebhookSyncService(Protocol):
    def run_record(self, record_id: str) -> SyncSummary: ...


def create_app(
    config: AppConfig | None = None,
    service: WebhookSyncService | None = None,
) -> Flask:
    """
    功能说明：创建接收飞书多维表自动化请求的 Flask 应用。

    参数：
        config：可选的应用配置；未传入时从 .env 和 config.toml 加载。
        service：可选的同步服务；测试时可传入替代实现。

    返回值：配置完成的 Flask 应用实例。
    """
    app_config = config or load_config()
    if not app_config.webhook_secret:
        raise ValueError("启动 Webhook 服务前必须配置 WEBHOOK_SECRET")
    configure_logging(app_config.log_level)
    logger = logging.getLogger("stocking_sheet_sync.web")
    owned_resources: tuple[FeishuClient, FeishuClient, RedisStateStore] | None = None

    if service is None:
        store = RedisStateStore(
            app_config.redis_url,
            app_config.redis_key_prefix,
            monitor_days=app_config.monitor_days,
            socket_timeout_seconds=app_config.request_timeout_seconds,
            logger=logger,
        )
        data_client = FeishuClient(
            app_config,
            app_config.feishu_data_app_id,
            app_config.feishu_data_app_secret,
            "data",
            logger,
        )
        message_client = FeishuClient(
            app_config,
            app_config.feishu_message_app_id,
            app_config.feishu_message_app_secret,
            "message",
            logger,
        )
        service = SyncService(
            app_config,
            data_client,
            message_client,
            store,
            logger,
        )
        owned_resources = (data_client, message_client, store)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    if owned_resources is not None:
        data_client, message_client, store = owned_resources

        def close_resources() -> None:
            data_client.close()
            message_client.close()
            store.close()

        atexit.register(close_resources)

    @app.get("/healthz")
    def healthz():
        return jsonify(
            {
                "status": "ok",
                "service": "stocking-sheet-sync",
            }
        )

    @app.post("/webhooks/base-record")
    def handle_base_record():
        if not _is_authorized(app_config.webhook_secret):
            logger.warning("Webhook 鉴权失败：remote_addr=%s", request.remote_addr)
            return jsonify({"status": "unauthorized"}), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return (
                jsonify(
                    {
                        "status": "invalid_request",
                        "message": "请求体必须是 JSON 对象",
                    }
                ),
                400,
            )

        record_id = str(payload.get("record_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", record_id):
            return jsonify(
                {
                    "status": "invalid_request",
                    "message": "record_id 为空或格式无效",
                }
            ), 400

        logger.info(
            "收到多维表自动化 Webhook：record_id=%s",
            record_id,
        )
        try:
            summary = service.run_record(record_id)
        except SyncBusyError:
            reason = "已有同步任务正在运行"
            logger.warning(
                "多维表自动化 Webhook 处理完成：record_id=%s result=busy reason=%s",
                record_id,
                reason,
            )
            return jsonify(
                {
                    "status": "busy",
                    "result": "busy",
                    "reason": reason,
                    "record_id": record_id,
                }
            ), 409
        except Exception as error:
            logger.error(
                "多维表自动化 Webhook 处理完成：record_id=%s "
                "result=failed reason=%s",
                record_id,
                error,
            )
            logger.debug(
                "多维表自动化 Webhook 处理异常堆栈：record_id=%s",
                record_id,
                exc_info=True,
            )
            return jsonify(
                {
                    "status": "error",
                    "result": "failed",
                    "reason": str(error),
                    "record_id": record_id,
                }
            ), 500

        response_status = "failed" if summary.result == "failed" else "success"
        status_code = 500 if summary.result == "failed" else 200
        if summary.reason:
            logger.info(
                "多维表自动化 Webhook 处理完成：record_id=%s result=%s reason=%s",
                record_id,
                summary.result,
                summary.reason,
            )
        else:
            logger.info(
                "多维表自动化 Webhook 处理完成：record_id=%s result=%s",
                record_id,
                summary.result,
            )
        return jsonify(
            {
                "status": response_status,
                "result": summary.result,
                "reason": summary.reason,
                "record_id": record_id,
                "summary": asdict(summary),
            }
        ), status_code

    logger.info(
        "Webhook 服务初始化完成：url=%s/webhooks/base-record",
        app_config.public_base_url,
    )
    return app


def _is_authorized(secret: str) -> bool:
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    return hmac.compare_digest(authorization, expected)

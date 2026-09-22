from __future__ import annotations

import atexit
import hmac
import logging
import re
from typing import Protocol

from flask import Flask, jsonify, request

from stocking_sheet_sync.infrastructure.queue import TaskQueue
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.settings import AppConfig, load_config


class WebhookQueue(Protocol):
    def enqueue(self, record_id: str) -> str: ...


def create_app(
    config: AppConfig | None = None,
    service: WebhookQueue | None = None,
) -> Flask:
    """
    功能说明：创建接收飞书多维表自动化请求的 Flask 应用。

    参数：
        config：可选的应用配置；未传入时从 .env 和 config/config.toml 加载。
        service：可选任务队列；测试时可传入替代实现。

    返回值：配置完成的 Flask 应用实例。
    """
    app_config = config or load_config()
    if not app_config.webhook_secret:
        raise ValueError("启动 Webhook 服务前必须配置 WEBHOOK_SECRET")
    configure_logging(app_config.log_level)
    logger = logging.getLogger("stocking_sheet_sync.entrypoints.web")
    owned_resources = None
    if service is None:
        service = TaskQueue(app_config)
        owned_resources = service

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

    if owned_resources is not None:
        atexit.register(owned_resources.close)

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

        if "force" in payload or "request_id" in payload:
            return jsonify(
                {
                    "status": "invalid_request",
                    "message": "Webhook 仅支持普通搬运，请使用 stocking-sheet-sync rerun 重新搬运",
                }
            ), 400

        logger.debug(
            "收到多维表自动化 Webhook：record_id=%s",
            record_id,
        )
        try:
            task_id = service.enqueue(record_id)
        except Exception:
            logger.exception("Webhook任务入队失败：record_id=%s", record_id)
            return jsonify(
                {"status": "error", "reason": "任务入队失败，请稍后重试", "record_id": record_id}
            ), 503
        logger.info("Webhook任务已接收：record_id=%s task_id=%s", record_id, task_id)
        return jsonify({"status": "accepted", "task_id": task_id, "record_id": record_id}), 202

    logger.info(
        "Webhook 服务初始化完成：url=%s/webhooks/base-record history=%s forecast=%s",
        app_config.public_base_url,
        app_config.fill_history_enabled,
        app_config.fill_forecast_enabled,
    )
    return app


def _is_authorized(secret: str) -> bool:
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    return hmac.compare_digest(authorization, expected)

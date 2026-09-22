from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from stocking_sheet_sync.entrypoints.web import create_app
from tests.services.test_sync_service import make_config


class FakeWebhookService:
    def __init__(self) -> None:
        self.record_ids: list[str] = []

    def enqueue(self, record_id: str) -> str:
        self.record_ids.append(record_id)
        return "123-0"


def test_webhook_requires_bearer_secret(tmp_path: Path) -> None:
    config = replace(make_config(tmp_path), webhook_secret="expected-secret")
    service = FakeWebhookService()
    app = create_app(config, service)
    client = app.test_client()

    response = client.post(
        "/webhooks/base-record",
        json={"record_id": "rec_test"},
        headers={"Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401
    assert service.record_ids == []


def test_webhook_processes_one_record(tmp_path: Path, caplog) -> None:
    config = replace(make_config(tmp_path), webhook_secret="expected-secret")
    service = FakeWebhookService()
    app = create_app(config, service)
    client = app.test_client()
    caplog.set_level(logging.INFO, logger="stocking_sheet_sync.entrypoints.web")
    caplog.clear()

    response = client.post(
        "/webhooks/base-record",
        json={"record_id": "rec_test"},
        headers={"Authorization": "Bearer expected-secret"},
    )

    assert response.status_code == 202
    assert response.get_json() == {
        "status": "accepted",
        "task_id": "123-0",
        "record_id": "rec_test",
    }

    assert service.record_ids == ["rec_test"]
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "stocking_sheet_sync.entrypoints.web"
    ]
    assert messages == [
        "收到多维表自动化 Webhook：record_id=rec_test",
        "Webhook任务已接收：record_id=rec_test task_id=123-0",
    ]


def test_webhook_rejects_missing_record_id(tmp_path: Path) -> None:
    config = replace(make_config(tmp_path), webhook_secret="expected-secret")
    service = FakeWebhookService()
    app = create_app(config, service)
    client = app.test_client()

    response = client.post(
        "/webhooks/base-record",
        json={},
        headers={"Authorization": "Bearer expected-secret"},
    )

    assert response.status_code == 400
    assert service.record_ids == []


def test_webhook_rejects_manual_rerun_options_before_running(tmp_path):
    service = FakeWebhookService()
    client = create_app(make_config(tmp_path), service).test_client()
    invalid_options = [
        {"force": True, "request_id": "valid-request"},
        {"force": False},
        {"force": "false"},
        {"force": 1},
        {"force": None},
        {"force": True},
        {"force": True, "request_id": ""},
        {"force": True, "request_id": 1},
        {"force": True, "request_id": "has:separator"},
        {"force": True, "request_id": "x" * 129},
        {"force": False, "request_id": "request-1"},
        {"request_id": "request-1"},
    ]
    for options in invalid_options:
        response = client.post(
            "/webhooks/base-record",
            json={"record_id": "rec_test", **options},
            headers={"Authorization": "Bearer webhook-secret"},
        )
        assert response.status_code == 400
    assert not service.record_ids


def test_queue_failure_returns_503_without_false_ack(tmp_path):
    class BrokenQueue:
        def enqueue(self, record_id):
            raise ConnectionError("Redis unavailable")

    client = create_app(make_config(tmp_path), BrokenQueue()).test_client()
    response = client.post(
        "/webhooks/base-record",
        json={"record_id": "rec_test"},
        headers={"Authorization": "Bearer webhook-secret"},
    )
    assert response.status_code == 503
    assert "task_id" not in response.get_json()

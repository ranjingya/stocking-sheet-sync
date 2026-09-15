from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from stocking_sheet_sync.models import SyncSummary
from stocking_sheet_sync.web import create_app
from tests.test_sync_service import make_config


class FakeWebhookService:
    def __init__(self) -> None:
        self.record_ids: list[str] = []

    def run_record(self, record_id: str) -> SyncSummary:
        self.record_ids.append(record_id)
        return SyncSummary(scanned=1, copied=1, result="copied")


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
    caplog.set_level(logging.INFO, logger="stocking_sheet_sync.web")
    caplog.clear()

    response = client.post(
        "/webhooks/base-record",
        json={"record_id": "rec_test"},
        headers={"Authorization": "Bearer expected-secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["result"] == "copied"
    assert response.get_json()["reason"] == ""
    assert response.get_json()["summary"]["copied"] == 1
    assert service.record_ids == ["rec_test"]
    messages = [
        record.getMessage() for record in caplog.records if record.name == "stocking_sheet_sync.web"
    ]
    assert messages == [
        "收到多维表自动化 Webhook：record_id=rec_test",
        "多维表自动化 Webhook 处理完成：record_id=rec_test result=copied",
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


def test_force_webhook_reuses_request_id(tmp_path):
    from tests.test_sync_service import make_service

    service, data_client, redis, clock = make_service(tmp_path)
    client = create_app(service.config, service).test_client()
    headers = {"Authorization": "Bearer webhook-secret"}
    payload = {"record_id": "rec_test", "force": True, "request_id": "request-1"}
    first = client.post("/webhooks/base-record", json=payload, headers=headers)
    again = client.post("/webhooks/base-record", json=payload, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json["result"] == "copied" and again.json["result"] == "unchanged"
    assert first.json["summary"]["request_id"] == "request-1"
    assert first.json["summary"]["force"] is True
    assert data_client.copy_count == 1


def test_force_webhook_validates_options_before_running(tmp_path):
    service = FakeWebhookService()
    client = create_app(make_config(tmp_path), service).test_client()
    invalid_options = [
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

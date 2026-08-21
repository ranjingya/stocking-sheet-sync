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

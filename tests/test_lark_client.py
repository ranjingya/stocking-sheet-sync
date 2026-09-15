import time

import httpx
import pytest

from stocking_sheet_sync.lark_client import CopyOutcomeUnknown, CopyRejected, FeishuClient
from tests.test_sync_service import make_config


@pytest.mark.parametrize(
    "outcome",
    ["timeout", "server_error", "missing_file", "invalid_json", "null_fields", "gateway_timeout"],
)
def test_copy_uncertain_response_is_never_retried(tmp_path, outcome):
    client = FeishuClient(make_config(tmp_path), "test-app", "test-secret", "test")
    client._client.close()
    requests = []

    def handler(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("响应超时", request=request)
        if outcome == "server_error":
            return httpx.Response(503, json={"code": 1, "msg": "服务异常"})
        if outcome == "null_fields":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "file": {
                            "token": None,
                            "url": None,
                        }
                    },
                },
            )
        if outcome == "gateway_timeout":
            return httpx.Response(408, json={"code": 1, "msg": "请求超时"})
        if outcome == "invalid_json":
            return httpx.Response(200, text="<html>异常响应</html>")
        return httpx.Response(200, json={"code": 0, "data": {}})

    client._client = httpx.Client(
        base_url="https://example.invalid", transport=httpx.MockTransport(handler)
    )
    client._access_token = "test-token"
    client._token_expires_at = time.monotonic() + 3600
    try:
        with pytest.raises(CopyOutcomeUnknown):
            client.copy_spreadsheet("source", "目标")
        assert len(requests) == 1
        assert requests[0].url.path.endswith("/source/copy")
    finally:
        client.close()


def test_explicit_copy_rejection_can_be_reported_for_retry(tmp_path):
    client = FeishuClient(make_config(tmp_path), "test-app", "test-secret", "test")
    client._client.close()
    client._client = httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(403, json={"code": 99991672, "msg": "没有权限"})
        ),
    )
    client._access_token = "test-token"
    client._token_expires_at = time.monotonic() + 3600
    try:
        with pytest.raises(CopyRejected, match="没有权限"):
            client.copy_spreadsheet("source", "目标")
    finally:
        client.close()


def test_read_request_still_retries(tmp_path, monkeypatch):
    client = FeishuClient(make_config(tmp_path), "test-app", "test-secret", "test")
    client._client.close()
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(503, json={"code": 1})
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    client._client = httpx.Client(
        base_url="https://example.invalid", transport=httpx.MockTransport(handler)
    )
    client._access_token = "test-token"
    client._token_expires_at = time.monotonic() + 3600
    monkeypatch.setattr("stocking_sheet_sync.lark_client.time.sleep", lambda _: None)
    try:
        assert client._request("GET", "/test") == {"ok": True}
        assert len(attempts) == 2
    finally:
        client.close()


def test_successful_copy_returns_target_identity(tmp_path):
    client = FeishuClient(make_config(tmp_path), "test-app", "test-secret", "test")
    client._client.close()
    client._client = httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "file": {
                            "token": "target",
                            "url": "https://example.feishu.cn/sheets/target",
                            "name": "目标",
                            "type": "sheet",
                        }
                    },
                },
            )
        ),
    )
    client._access_token = "test-token"
    client._token_expires_at = time.monotonic() + 3600
    try:
        result = client.copy_spreadsheet("source", "目标")
        assert result.token == "target"
        assert result.name == "目标"
        assert result.url == "https://example.feishu.cn/sheets/target"
    finally:
        client.close()


def test_copy_uses_explicit_backup_folder_without_changing_default(tmp_path, monkeypatch):
    client = FeishuClient(make_config(tmp_path), "app", "secret", "test")
    calls = []

    def request(method, path, **kwargs):
        calls.append(kwargs)
        return {"file": {"token": "copied", "url": "https://example.feishu.cn/sheets/copied"}}

    monkeypatch.setattr(client, "_request", request)
    try:
        client.copy_spreadsheet("source", "原始备份", folder_token="backup")
        client.copy_spreadsheet("source", "交付")
        assert [c["json_body"]["folder_token"] for c in calls] == ["backup", "folder-token"]
        assert all(c["retry"] is False for c in calls)
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["success", "timeout", "mismatch", "already_named"])
def test_native_rename_verifies_title_and_recovers_lost_response(tmp_path, mode):
    import json

    client = FeishuClient(make_config(tmp_path), "app", "secret", "test")
    client._client.close()
    title = "完成" if mode == "already_named" else "未完成"
    patches = []

    def handler(request):
        nonlocal title
        assert request.url.path == "/open-apis/sheets/v3/spreadsheets/backup"
        if request.method == "PATCH":
            patches.append(json.loads(request.content))
            if mode != "mismatch":
                title = patches[-1]["title"]
            if mode == "timeout":
                raise httpx.ReadTimeout("响应丢失", request=request)
            return httpx.Response(200, json={"code": 0, "data": {}})
        assert request.method == "GET"
        return httpx.Response(200, json={"code": 0, "data": {"spreadsheet": {"title": title}}})

    client._client = httpx.Client(
        base_url="https://example.invalid", transport=httpx.MockTransport(handler)
    )
    client._access_token = "test-token"
    client._token_expires_at = time.monotonic() + 3600
    try:
        if mode in {"timeout", "mismatch"}:
            with pytest.raises((httpx.ReadTimeout, RuntimeError)):
                client.rename_spreadsheet("backup", "完成")
            assert len(patches) == 1
            if mode == "timeout":
                client.rename_spreadsheet("backup", "完成")
                assert len(patches) == 1
        else:
            client.rename_spreadsheet("backup", "完成")
            assert len(patches) == (0 if mode == "already_named" else 1)
        assert all(body == {"title": "完成"} for body in patches)
    finally:
        client.close()

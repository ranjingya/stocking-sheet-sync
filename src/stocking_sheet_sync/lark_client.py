from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from .config import AppConfig
from .models import BaseRecord, CopyResult


class FeishuApiError(RuntimeError):
    def __init__(self, message: str, code: int, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FeishuClient:
    def __init__(
        self,
        config: AppConfig,
        app_id: str,
        app_secret: str,
        client_name: str,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        功能说明：创建使用指定飞书应用身份的 OpenAPI 客户端。

        参数：
            config：通用接口地址、超时和重试配置。
            app_id：该客户端使用的飞书应用 App ID。
            app_secret：该客户端使用的飞书应用 App Secret。
            client_name：日志中用于区分应用用途的名称。
            logger：可选日志记录器。

        返回值：无。
        """
        self.config = config
        self.app_id = app_id
        self.app_secret = app_secret
        self.client_name = client_name
        self.logger = logger or logging.getLogger(__name__)
        self._access_token = ""
        self._token_expires_at = 0.0
        self._client = httpx.Client(
            base_url=config.feishu_api_base_url,
            timeout=config.request_timeout_seconds,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    def close(self) -> None:
        self._client.close()

    def list_base_records(self) -> list[BaseRecord]:
        """
        功能说明：分页读取目标多维表中的全部候选记录。

        参数：无。

        返回值：标准化后的多维表记录列表。
        """
        records: list[BaseRecord] = []
        page_token = ""
        while True:
            params: dict[str, str] = {
                "page_size": "200",
                "automatic_fields": "true",
                "user_id_type": "open_id",
            }
            if page_token:
                params["page_token"] = page_token
            if self.config.base_view_id:
                params["view_id"] = self.config.base_view_id

            path = (
                f"/open-apis/bitable/v1/apps/{quote(self.config.base_app_token, safe='')}"
                f"/tables/{quote(self.config.base_table_id, safe='')}/records"
            )
            data = self._request("GET", path, params=params)
            items = data.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError("多维表接口返回的 records.items 不是列表")
            for item in items:
                record = _parse_base_record(item)
                if record is not None:
                    records.append(record)

            has_more = bool(data.get("has_more"))
            page_token = str(data.get("page_token", "")).strip() if has_more else ""
            if not page_token:
                break

        self.logger.debug("已读取多维表格候选记录：record_count=%d", len(records))
        return records

    def get_base_record(self, record_id: str) -> BaseRecord:
        """
        功能说明：根据记录 ID 读取一条多维表记录，供 Webhook 精确触发同步。

        参数：
            record_id：多维表记录 ID。

        返回值：标准化后的单条多维表记录。
        """
        path = (
            f"/open-apis/bitable/v1/apps/{quote(self.config.base_app_token, safe='')}"
            f"/tables/{quote(self.config.base_table_id, safe='')}"
            f"/records/{quote(record_id, safe='')}"
        )
        data = self._request(
            "GET",
            path,
            params={
                "automatic_fields": "true",
                "user_id_type": "open_id",
                "with_shared_url": "true",
            },
        )
        record = _parse_base_record(data.get("record"))
        if record is None:
            raise RuntimeError(f"多维表接口未返回有效记录：{record_id}")
        self.logger.debug("已读取 Webhook 触发记录：record_id=%s", record.record_id)
        return record

    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]:
        """
        功能说明：将 Wiki 节点解析为其背后的真实云文档。

        参数：
            wiki_token：Wiki 节点 token。

        返回值：真实文档 token、文档类型和标题。
        """
        data = self._request(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": wiki_token, "obj_type": "wiki"},
        )
        node = data.get("node")
        if not isinstance(node, dict):
            raise RuntimeError(f"Wiki 节点未返回真实文档信息：{wiki_token}")
        token = str(node.get("obj_token", "")).strip()
        document_type = str(node.get("obj_type", "")).strip()
        title = str(node.get("title", "")).strip() or "未命名表格"
        if not token or not document_type:
            raise RuntimeError(f"Wiki 节点未返回真实文档信息：{wiki_token}")
        return token, document_type, title

    def get_spreadsheet_revision(self, spreadsheet_token: str) -> tuple[int, str]:
        """
        功能说明：读取电子表格当前 revision 和标题，用于识别内容变化。

        参数：
            spreadsheet_token：真实电子表格 token。

        返回值：当前 revision 和接口返回的标题。
        """
        data = self._request(
            "GET",
            f"/open-apis/sheets/v2/spreadsheets/{quote(spreadsheet_token, safe='')}/metainfo",
        )
        properties = data.get("properties") if isinstance(data.get("properties"), dict) else {}
        spreadsheet = data.get("spreadsheet") if isinstance(data.get("spreadsheet"), dict) else {}
        revision = data.get("revision", properties.get("revision", spreadsheet.get("revision")))
        title = str(properties.get("title", spreadsheet.get("title", ""))).strip()
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RuntimeError(f"电子表格未返回有效 revision：{spreadsheet_token}")
        return revision, title

    def copy_spreadsheet(self, spreadsheet_token: str, copy_name: str) -> CopyResult:
        """
        功能说明：把源电子表格复制到配置的共享文件夹。

        参数：
            spreadsheet_token：源电子表格 token。
            copy_name：副本文件名。

        返回值：新副本的名称、token、类型和链接。
        """
        data = self._request(
            "POST",
            f"/open-apis/drive/v1/files/{quote(spreadsheet_token, safe='')}/copy",
            json_body={
                "folder_token": self.config.target_folder_token,
                "name": copy_name,
                "type": "sheet",
            },
        )
        file_data = data.get("file")
        if not isinstance(file_data, dict):
            raise RuntimeError(f"复制接口未返回目标文件信息：{spreadsheet_token}")
        token = str(file_data.get("token", "")).strip()
        url = str(file_data.get("url", "")).strip()
        if not token or not url:
            raise RuntimeError(f"复制接口未返回目标文件信息：{spreadsheet_token}")
        return CopyResult(
            name=str(file_data.get("name", "")).strip() or copy_name,
            token=token,
            file_type=str(file_data.get("type", "sheet")),
            url=url,
        )

    def send_card(self, open_id: str, card: dict[str, Any]) -> None:
        """
        功能说明：以应用机器人身份向一个用户发送交互式卡片。

        参数：
            open_id：当前自建应用体系下的用户 open_id。
            card：飞书 Card 2.0 JSON 对象。

        返回值：无；调用失败时抛出异常。
        """
        import json

        self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json_body={
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
            },
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                token = self._get_access_token()
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
                payload = self._decode_payload(response)
                code = int(payload.get("code", -1))
                if response.is_success and code == 0:
                    data = payload.get("data", {})
                    if not isinstance(data, dict):
                        raise RuntimeError("飞书接口返回的 data 不是对象")
                    return data

                error = FeishuApiError(
                    f"飞书接口失败：{payload.get('msg') or response.reason_phrase}",
                    code,
                    response.status_code,
                )
                if attempt >= self.config.max_retries or not _should_retry(
                    response.status_code, code
                ):
                    raise error
                last_error = error
            except FeishuApiError as error:
                last_error = error
                if attempt >= self.config.max_retries or not _should_retry(
                    error.status, error.code
                ):
                    raise
            except (httpx.TransportError, httpx.TimeoutException) as error:
                last_error = error
                if attempt >= self.config.max_retries:
                    raise

            wait_seconds = 0.5 * (2 ** (attempt - 1))
            self.logger.warning(
                "飞书接口调用失败，准备重试：attempt=%d wait_seconds=%.1f path=%s",
                attempt,
                wait_seconds,
                path,
            )
            time.sleep(wait_seconds)

        if last_error:
            raise last_error
        raise RuntimeError("飞书接口调用失败")

    def _get_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at - 60:
            return self._access_token

        response = self._client.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            },
        )
        payload = self._decode_payload(response)
        token = str(payload.get("tenant_access_token", "")).strip()
        code = int(payload.get("code", -1))
        if not response.is_success or code != 0 or not token:
            detail = payload.get("msg") or response.reason_phrase
            raise FeishuApiError(
                f"获取 tenant_access_token 失败：{detail}",
                code,
                response.status_code,
            )

        expire = payload.get("expire", 7200)
        expire_seconds = int(expire) if isinstance(expire, int | float | str) else 7200
        self._access_token = token
        self._token_expires_at = time.monotonic() + expire_seconds
        self.logger.debug(
            "已刷新飞书 tenant_access_token：client=%s",
            self.client_name,
        )
        return token

    @staticmethod
    def _decode_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError(
                f"飞书接口返回非 JSON 数据：status={response.status_code}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError("飞书接口返回内容不是 JSON 对象")
        return payload


def _should_retry(status: int, code: int) -> bool:
    return status == 429 or status >= 500 or code in {99991400, 99991401}


def _parse_base_record(value: object) -> BaseRecord | None:
    if not isinstance(value, dict):
        return None
    record_id = str(value.get("record_id", "")).strip()
    fields = value.get("fields", {})
    if not record_id or not isinstance(fields, dict):
        return None
    return BaseRecord(
        record_id=record_id,
        fields=fields,
        shared_url=str(value.get("shared_url") or value.get("record_url") or "").strip(),
    )

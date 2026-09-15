from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .config import AppConfig
from .models import BaseRecord, CopyResult


class FeishuApiError(RuntimeError):
    def __init__(self, message: str, code: int, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class CopyRejected(RuntimeError):
    """飞书明确拒绝复制，可以重新触发。"""


class CopyOutcomeUnknown(RuntimeError):
    """复制结果无法确认，需要人工核对目标文件夹。"""


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

    def copy_spreadsheet(
        self, spreadsheet_token: str, copy_name: str, *, folder_token: str | None = None
    ) -> CopyResult:
        """
        功能说明：把源电子表格复制到配置的共享文件夹。

        参数：
            spreadsheet_token：源电子表格 token。
            copy_name：副本文件名。
            folder_token：可选目标文件夹；未指定时使用配置的交付文件夹。

        返回值：新副本的名称、token、类型和链接。
        """
        try:
            data = self._request(
                "POST",
                f"/open-apis/drive/v1/files/{quote(spreadsheet_token, safe='')}/copy",
                retry=False,
                json_body={
                    "folder_token": folder_token or self.config.target_folder_token,
                    "name": copy_name,
                    "type": "sheet",
                },
            )
            file_data = data.get("file")
            if not isinstance(file_data, dict):
                raise RuntimeError(f"复制接口未返回目标文件信息：{spreadsheet_token}")
            raw_token = file_data.get("token")
            raw_url = file_data.get("url")
            token = raw_token.strip() if isinstance(raw_token, str) else ""
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            parsed_url = urlsplit(url)
            if not token or parsed_url.scheme != "https" or not parsed_url.netloc:
                raise RuntimeError(f"复制接口未返回目标文件信息：{spreadsheet_token}")
            return CopyResult(
                name=str(file_data.get("name", "")).strip() or copy_name,
                token=token,
                file_type=str(file_data.get("type", "sheet")),
                url=url,
            )
        except FeishuApiError as error:
            if (400 <= error.status < 500 and error.status not in {408, 499}) or (
                200 <= error.status < 300 and error.code > 0
            ):
                raise CopyRejected(str(error)) from error
            raise CopyOutcomeUnknown("复制结果不确定，请核对目标文件夹") from error
        except Exception as error:
            raise CopyOutcomeUnknown("复制结果不确定，请核对目标文件夹") from error

    def rename_spreadsheet(self, spreadsheet_token: str, title: str) -> None:
        """
        功能说明：通过服务端接口设置电子表格标题，并回读核验；标题相同时跳过写入。

        参数：
            spreadsheet_token：待改名的处理备份 token。
            title：完整目标标题。
        返回值：无；接口失败或回读不一致时抛错，可在同一批次中再次核对。
        """
        if not spreadsheet_token or not title.strip():
            raise ValueError("表格 token 和标题不能为空")
        path = f"/open-apis/sheets/v3/spreadsheets/{quote(spreadsheet_token, safe='')}"
        current = self._request("GET", path)["spreadsheet"]["title"]
        if current == title:
            self.logger.info("备份标题已匹配：token=%s title=%s", spreadsheet_token, title)
            return
        self.logger.info("更新备份标题：token=%s title=%s", spreadsheet_token, title)
        self._request("PATCH", path, retry=False, json_body={"title": title})
        actual = self._request("GET", path)["spreadsheet"]["title"]
        if actual != title:
            raise RuntimeError("备份标题回读不一致，请重试同一批次核对")
        self.logger.info("备份标题核验完成：token=%s", spreadsheet_token)

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
        retry: bool = True,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        attempts = self.config.max_retries if retry else 1
        for attempt in range(1, attempts + 1):
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
                if attempt >= attempts or not _should_retry(response.status_code, code):
                    raise error
                last_error = error
            except FeishuApiError as error:
                last_error = error
                if attempt >= attempts or not _should_retry(error.status, error.code):
                    raise
            except (httpx.TransportError, httpx.TimeoutException) as error:
                last_error = error
                if attempt >= attempts:
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

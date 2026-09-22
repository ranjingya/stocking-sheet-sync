from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from stocking_sheet_sync.settings import AppConfig

from .base import BaseOperations
from .drive import DriveOperations
from .errors import CopyOutcomeUnknown as CopyOutcomeUnknown
from .errors import CopyRejected as CopyRejected
from .errors import FeishuApiError as FeishuApiError


class FeishuClient(BaseOperations, DriveOperations):
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

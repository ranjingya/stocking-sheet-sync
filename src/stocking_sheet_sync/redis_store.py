from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from redis import Redis

from .models import SyncState, SyncStatus


_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisStateStore:
    def __init__(
        self,
        redis_url: str,
        key_prefix: str,
        *,
        socket_timeout_seconds: float = 5,
        client: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._key_prefix = key_prefix.rstrip(":")
        if not self._key_prefix:
            raise ValueError("Redis key_prefix 不能为空")
        self._lock_token: str | None = None
        self._redis = client or Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=socket_timeout_seconds,
            socket_connect_timeout=socket_timeout_seconds,
            health_check_interval=30,
        )
        self._redis.ping()
        self._logger.info("Redis 状态存储连接成功：key_prefix=%s", self._key_prefix)

    def acquire_run_lock(self, ttl_minutes: float) -> bool:
        """
        功能说明：通过 Redis SET NX EX 获取分布式扫描锁。

        参数：
            ttl_minutes：锁自动失效的分钟数。

        返回值：
            是否成功取得锁。
        """
        token = uuid.uuid4().hex
        ttl_seconds = max(1, math.ceil(ttl_minutes * 60))
        acquired = bool(
            self._redis.set(self._lock_key, token, nx=True, ex=ttl_seconds)
        )
        self._lock_token = token if acquired else None
        return acquired

    def release_run_lock(self) -> None:
        token = self._lock_token
        self._lock_token = None
        if token:
            self._redis.eval(_RELEASE_LOCK_SCRIPT, 1, self._lock_key, token)

    def get(self, record_id: str) -> SyncState | None:
        values = self._redis.hgetall(self._state_key(record_id))
        if not values:
            return None
        return _map_hash(values)

    def save_baseline(
        self,
        *,
        record_id: str,
        source_token: str,
        source_revision: int,
        original_name: str,
        record_url: str,
    ) -> None:
        """
        功能说明：保存初次接管时的 revision 基线，不复制也不通知。

        参数：
            record_id：多维表记录 ID。
            source_token：真实电子表格 token。
            source_revision：当前 revision。
            original_name：源表格名称。
            record_url：多维表记录链接。

        返回值：无。
        """
        self.save(
            SyncState(
                record_id=record_id,
                source_token=source_token,
                source_revision=source_revision,
                original_name=original_name,
                record_url=record_url,
                status="baseline",
                updated_at=datetime.now(UTC).isoformat(),
            )
        )

    def save(self, state: SyncState) -> None:
        self._redis.hset(self._state_key(state.record_id), mapping=_state_mapping(state))

    def update_pending_notifications(self, record_id: str, open_ids: list[str]) -> None:
        key = self._state_key(record_id)
        if not self._redis.exists(key):
            self._logger.warning("待通知状态不存在，无法更新：record_id=%s", record_id)
            return
        self._redis.hset(
            key,
            mapping={
                "pending_notify_open_ids": json.dumps(open_ids, ensure_ascii=False),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    def close(self) -> None:
        self._redis.close()

    @property
    def _lock_key(self) -> str:
        return f"{self._key_prefix}:lock:scan"

    def _state_key(self, record_id: str) -> str:
        return f"{self._key_prefix}:state:{record_id}"


def _state_mapping(state: SyncState) -> dict[str, str]:
    return {
        "record_id": state.record_id,
        "source_token": state.source_token,
        "source_revision": str(state.source_revision),
        "original_name": state.original_name,
        "record_url": state.record_url,
        "target_token": state.target_token or "",
        "target_name": state.target_name or "",
        "target_url": state.target_url or "",
        "copied_at": state.copied_at or "",
        "status": state.status,
        "pending_notify_open_ids": json.dumps(
            state.pending_notify_open_ids, ensure_ascii=False
        ),
        "last_error": state.last_error or "",
        "last_error_notified_at": state.last_error_notified_at or "",
        "updated_at": state.updated_at,
    }


def _map_hash(values: dict[str, str]) -> SyncState:
    status = values.get("status", "error")
    if status not in {"success", "error", "baseline"}:
        raise ValueError(f"Redis 中存在不支持的同步状态：{status}")
    try:
        pending = json.loads(values.get("pending_notify_open_ids", "[]"))
    except json.JSONDecodeError:
        pending = []
    if not isinstance(pending, list):
        pending = []

    try:
        source_revision = int(values.get("source_revision", "-1"))
    except ValueError as error:
        raise ValueError("Redis 中的 source_revision 不是整数") from error

    return SyncState(
        record_id=values.get("record_id", ""),
        source_token=values.get("source_token", ""),
        source_revision=source_revision,
        original_name=values.get("original_name", ""),
        record_url=values.get("record_url", ""),
        target_token=values.get("target_token") or None,
        target_name=values.get("target_name") or None,
        target_url=values.get("target_url") or None,
        copied_at=values.get("copied_at") or None,
        status=cast(SyncStatus, status),
        pending_notify_open_ids=[item for item in pending if isinstance(item, str)],
        last_error=values.get("last_error") or None,
        last_error_notified_at=values.get("last_error_notified_at") or None,
        updated_at=values.get("updated_at", ""),
    )

from __future__ import annotations

import json
import logging
import math
import uuid
from typing import Any

from redis import Redis

from .models import SyncedRecord, SyncedSheetState

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
        self._logger.debug("Redis 状态存储连接成功：key_prefix=%s", self._key_prefix)

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
        acquired = bool(self._redis.set(self._lock_key, token, nx=True, ex=ttl_seconds))
        self._lock_token = token if acquired else None
        return acquired

    def release_run_lock(self) -> None:
        token = self._lock_token
        self._lock_token = None
        if token:
            self._redis.eval(_RELEASE_LOCK_SCRIPT, 1, self._lock_key, token)

    def is_synced(self, record_id: str, source_token: str, source_revision: int) -> bool:
        """
        功能说明：判断指定记录的源表格版本是否已经同步成功。

        参数：
            record_id：多维表记录 ID。
            source_token：真实电子表格 token。
            source_revision：电子表格 revision。

        返回值：
            Redis Hash 中是否存在对应同步标识。
        """
        return bool(
            self._redis.hexists(
                self._synced_key,
                make_sync_id(record_id, source_token, source_revision),
            )
        )

    def save_synced(self, record: SyncedRecord) -> None:
        """
        功能说明：记录一个已经同步完成的源表格版本。

        参数：
            record：源表格、原记录、目标链接和同步时间。

        返回值：无。
        """
        sync_id = make_sync_id(
            record.record_id,
            record.source_token,
            record.source_revision,
        )
        value = {
            "source_name": record.source_name,
            "source_url": record.source_url,
            "record_url": record.record_url,
            "target_name": record.target_name,
            "target_url": record.target_url,
            "synced_at": record.synced_at,
            "copy_version": record.copy_version,
        }
        self._redis.hset(
            self._synced_key,
            sync_id,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    def next_copy_version(self, record_id: str, source_token: str) -> int:
        """
        功能说明：计算同一多维表记录和源表格的下一次搬运版本号。

        参数：
            record_id：多维表记录 ID。
            source_token：真实电子表格 token。

        返回值：
            下一次成功搬运应使用的业务版本号；首次搬运返回 1。
        """
        successful_count = 0
        highest_recorded_version = 0
        for sync_id, raw in self._redis.hgetall(self._synced_key).items():
            parsed = parse_sync_id(sync_id)
            if parsed is None or parsed[:2] != (record_id, source_token):
                continue
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or not _string_value(value.get("target_url")):
                continue

            successful_count += 1
            copy_version = _positive_int(value.get("copy_version"))
            if copy_version is not None:
                highest_recorded_version = max(highest_recorded_version, copy_version)

        return max(successful_count, highest_recorded_version) + 1

    def get_synced(
        self, record_id: str, source_token: str, source_revision: int
    ) -> dict[str, str] | None:
        raw = self._redis.hget(
            self._synced_key,
            make_sync_id(record_id, source_token, source_revision),
        )
        if not raw:
            return None
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Redis 中的同步记录不是 JSON 对象")
        return {
            str(key): str(item)
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        }

    def list_latest_synced(self) -> list[SyncedSheetState]:
        """
        功能说明：读取全部同步历史，并按记录和源表格归并为最新同步状态。

        参数：无。

        返回值：
            每个 record_id 与 source_token 组合的最新 revision 及其业务信息。
        """
        latest: dict[tuple[str, str], SyncedSheetState] = {}
        for sync_id, raw in self._redis.hgetall(self._synced_key).items():
            parsed = parse_sync_id(sync_id)
            if parsed is None:
                self._logger.warning("忽略格式无效的 Redis 同步标识：sync_id=%s", sync_id)
                continue
            record_id, source_token, revision = parsed
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                self._logger.warning("忽略内容无效的 Redis 同步记录：sync_id=%s", sync_id)
                continue
            if not isinstance(value, dict):
                self._logger.warning("忽略非对象类型的 Redis 同步记录：sync_id=%s", sync_id)
                continue

            state = SyncedSheetState(
                record_id=record_id,
                source_token=source_token,
                synced_revision=revision,
                source_name=_string_value(value.get("source_name")),
                source_url=_string_value(value.get("source_url")),
                record_url=_string_value(value.get("record_url")),
                target_name=_string_value(value.get("target_name")),
                target_url=_string_value(value.get("target_url")),
                synced_at=_string_value(value.get("synced_at")),
                pending_revision=_optional_int(value.get("pending_revision")),
                pending_since=_string_value(value.get("pending_since")),
            )
            group = (record_id, source_token)
            current = latest.get(group)
            if current is None or state.synced_revision > current.synced_revision:
                latest[group] = state

        return sorted(latest.values(), key=lambda item: (item.record_id, item.source_token))

    def save_pending(
        self,
        state: SyncedSheetState,
        pending_revision: int | None,
        pending_since: str = "",
    ) -> None:
        """
        功能说明：在最新同步记录中保存或清除源表格静默观察状态。

        参数：
            state：当前最新同步状态，用于定位 Redis Hash field。
            pending_revision：观察中的 revision；传入 None 时清除观察状态。
            pending_since：当前 revision 开始保持不变的时间。

        返回值：无。
        """
        sync_id = make_sync_id(
            state.record_id,
            state.source_token,
            state.synced_revision,
        )
        raw = self._redis.hget(self._synced_key, sync_id)
        if not raw:
            raise ValueError(f"Redis 同步记录不存在：{sync_id}")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"Redis 同步记录不是 JSON 对象：{sync_id}")

        if pending_revision is None:
            value.pop("pending_revision", None)
            value.pop("pending_since", None)
        else:
            value["pending_revision"] = pending_revision
            value["pending_since"] = pending_since
        self._redis.hset(
            self._synced_key,
            sync_id,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

    def close(self) -> None:
        self._redis.close()

    @property
    def _lock_key(self) -> str:
        return f"{self._key_prefix}:lock:scan"

    @property
    def _synced_key(self) -> str:
        return f"{self._key_prefix}:synced"


def make_sync_id(record_id: str, source_token: str, source_revision: int) -> str:
    return f"{record_id}:{source_token}:{source_revision}"


def parse_sync_id(sync_id: str) -> tuple[str, str, int] | None:
    parts = sync_id.split(":")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    try:
        revision = int(parts[2])
    except ValueError:
        return None
    if revision < 0:
        return None
    return parts[0], parts[1], revision


def _string_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value

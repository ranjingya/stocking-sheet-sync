from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, timezone
from typing import Any

from redis import Redis

from .models import SyncedRecord, SyncedSheetState

_SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
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
        monitor_days: int = 3,
        legacy_hash_key: str | None = None,
        socket_timeout_seconds: float = 5,
        client: Any | None = None,
        logger: logging.Logger | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._key_prefix = key_prefix.rstrip(":")
        if not self._key_prefix:
            raise ValueError("Redis key_prefix 不能为空")
        if monitor_days < 1:
            raise ValueError("Redis monitor_days 必须为正整数")
        self._monitor_days = monitor_days
        self._legacy_hash_key = (legacy_hash_key or "").strip()
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
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
        self._migrate_legacy_hash()

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

    def get_state(self, record_id: str, source_token: str) -> SyncedSheetState | None:
        """
        功能说明：读取一条多维表记录与真实电子表格对应的最新监听状态。

        参数：
            record_id：多维表记录 ID。
            source_token：真实电子表格 token。

        返回值：
            当前监听状态；Key 不存在、已过期或内容无效时返回 None。
        """
        key = self._state_key(record_id, source_token)
        raw = self._redis.get(key)
        if not raw:
            return None
        state = self._parse_state(key, raw)
        if state is None:
            return None
        if self._is_expired(state):
            self._redis.delete(key)
            return None
        return state

    def is_synced(self, record_id: str, source_token: str, source_revision: int) -> bool:
        """
        功能说明：判断指定源表格 revision 是否已经成功搬运。

        参数：
            record_id：多维表记录 ID。
            source_token：真实电子表格 token。
            source_revision：电子表格 revision。

        返回值：
            当前状态的版本列表中是否存在对应 revision。
        """
        state = self.get_state(record_id, source_token)
        if state is None:
            return False
        return state.synced_revision == source_revision or any(
            _optional_int(version.get("revision")) == source_revision
            for version in state.versions
        )

    def save_synced(self, record: SyncedRecord) -> None:
        """
        功能说明：将一次成功搬运追加到 String Value 的版本列表并更新最新状态。

        参数：
            record：本次源表格、目标副本、版本号和同步时间。

        返回值：无。
        """
        current = self.get_state(record.record_id, record.source_token)
        versions = list(current.versions if current else ())
        versions.append(
            {
                "revision": record.source_revision,
                "copy_version": record.copy_version,
                "target_name": record.target_name,
                "target_url": record.target_url,
                "synced_at": record.synced_at,
            }
        )
        monitor_started_at = (
            current.monitor_started_at
            if current
            else record.monitor_started_at or record.synced_at
        )
        monitor_expires_at = (
            current.monitor_expires_at
            if current
            else record.monitor_expires_at
            or format_state_time(self._monitor_expiration(monitor_started_at))
        )
        state = SyncedSheetState(
            record_id=record.record_id,
            source_token=record.source_token,
            synced_revision=record.source_revision,
            source_name=record.source_name,
            source_url=record.source_url,
            record_url=record.record_url,
            target_name=record.target_name,
            target_url=record.target_url,
            synced_at=record.synced_at,
            copy_version=record.copy_version,
            monitor_started_at=monitor_started_at,
            monitor_expires_at=monitor_expires_at,
            versions=tuple(versions),
        )
        self._write_state(state)

    def next_copy_version(self, record_id: str, source_token: str) -> int:
        """
        功能说明：读取当前 String Value 并计算下一次成功搬运版本号。

        参数：
            record_id：多维表记录 ID。
            source_token：真实电子表格 token。

        返回值：
            首次搬运返回 1，后续返回当前最高搬运版本号加一。
        """
        state = self.get_state(record_id, source_token)
        if state is None:
            return 1
        recorded_versions = [
            version
            for item in state.versions
            if (version := _positive_int(item.get("copy_version"))) is not None
        ]
        highest = max(recorded_versions, default=state.copy_version)
        return max(highest, len(state.versions)) + 1

    def get_synced(
        self, record_id: str, source_token: str, source_revision: int
    ) -> dict[str, str] | None:
        state = self.get_state(record_id, source_token)
        if state is None:
            return None
        for version in state.versions:
            if _optional_int(version.get("revision")) == source_revision:
                return {
                    str(key): str(item)
                    for key, item in version.items()
                    if isinstance(key, str)
                }
        return None

    def list_latest_synced(self) -> list[SyncedSheetState]:
        """
        功能说明：扫描当前命名空间内所有未过期的表格监听状态。

        参数：无。

        返回值：
            每个 record_id 与 source_token 组合对应的一条最新状态。
        """
        states: list[SyncedSheetState] = []
        for key in self._redis.scan_iter(match=f"{self._key_prefix}:*"):
            if key == self._lock_key:
                continue
            raw = self._redis.get(key)
            if not raw:
                continue
            state = self._parse_state(key, raw)
            if state is None:
                continue
            if self._is_expired(state):
                self._redis.delete(key)
                continue
            states.append(state)
        return sorted(states, key=lambda item: (item.record_id, item.source_token))

    def save_pending(
        self,
        state: SyncedSheetState,
        pending_revision: int | None,
        pending_since: str = "",
    ) -> None:
        """
        功能说明：保存或清除一张已监听表格的静默观察状态。

        参数：
            state：当前监听状态。
            pending_revision：观察中的 revision；传入 None 时清除。
            pending_since：该 revision 最近一次变化时间。

        返回值：无。
        """
        current = self.get_state(state.record_id, state.source_token)
        if current is None:
            raise ValueError(
                f"Redis 监听状态不存在：{state.record_id}:{state.source_token}"
            )
        updated = replace(
            current,
            pending_revision=pending_revision,
            pending_since=pending_since if pending_revision is not None else "",
        )
        self._write_state(updated)

    def close(self) -> None:
        self._redis.close()

    @property
    def _lock_key(self) -> str:
        return f"{self._key_prefix}:lock:scan"

    def _state_key(self, record_id: str, source_token: str) -> str:
        return f"{self._key_prefix}:{record_id}:{source_token}"

    def _write_state(self, state: SyncedSheetState, *, nx: bool = False) -> bool:
        key = self._state_key(state.record_id, state.source_token)
        value = {
            "record_id": state.record_id,
            "source_token": state.source_token,
            "source_name": state.source_name,
            "source_url": state.source_url,
            "record_url": state.record_url,
            "synced_revision": state.synced_revision,
            "copy_version": state.copy_version,
            "target_name": state.target_name,
            "target_url": state.target_url,
            "synced_at": state.synced_at,
            "monitor_started_at": state.monitor_started_at,
            "monitor_expires_at": state.monitor_expires_at,
            "pending_revision": state.pending_revision,
            "pending_since": state.pending_since,
            "versions": list(state.versions),
        }
        written = bool(
            self._redis.set(
                key,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                nx=nx,
            )
        )
        if written:
            expires_at = parse_state_time(state.monitor_expires_at)
            if expires_at is not None:
                self._redis.expireat(key, math.ceil(expires_at.timestamp()))
        return written

    def _parse_state(self, key: str, raw: str) -> SyncedSheetState | None:
        parsed_key = self._parse_state_key(key)
        if parsed_key is None:
            return None
        record_id, source_token = parsed_key
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            self._logger.warning("忽略内容无效的 Redis String：key=%s", key)
            return None
        if not isinstance(value, dict):
            self._logger.warning("忽略非对象类型的 Redis String：key=%s", key)
            return None
        raw_versions = value.get("versions", [])
        versions = tuple(item for item in raw_versions if isinstance(item, dict))
        synced_revision = _optional_int(value.get("synced_revision"))
        if synced_revision is None:
            self._logger.warning("忽略缺少 synced_revision 的 Redis String：key=%s", key)
            return None
        return SyncedSheetState(
            record_id=record_id,
            source_token=source_token,
            synced_revision=synced_revision,
            source_name=_string_value(value.get("source_name")),
            source_url=_string_value(value.get("source_url")),
            record_url=_string_value(value.get("record_url")),
            target_name=_string_value(value.get("target_name")),
            target_url=_string_value(value.get("target_url")),
            synced_at=_string_value(value.get("synced_at")),
            copy_version=_positive_int(value.get("copy_version")) or max(len(versions), 1),
            monitor_started_at=_string_value(value.get("monitor_started_at")),
            monitor_expires_at=_string_value(value.get("monitor_expires_at")),
            pending_revision=_optional_int(value.get("pending_revision")),
            pending_since=_string_value(value.get("pending_since")),
            versions=versions,
        )

    def _parse_state_key(self, key: str) -> tuple[str, str] | None:
        prefix = f"{self._key_prefix}:"
        if not key.startswith(prefix) or key == self._lock_key:
            return None
        remainder = key[len(prefix) :]
        record_id, separator, source_token = remainder.partition(":")
        if not separator or not record_id or not source_token:
            return None
        return record_id, source_token

    def _monitor_expiration(self, started_at: str) -> datetime:
        parsed = parse_state_time(started_at) or self._now()
        local_date = parsed.astimezone(_SHANGHAI_TIMEZONE).date()
        expires_on = local_date + timedelta(days=self._monitor_days)
        return datetime.combine(expires_on, time.min, tzinfo=_SHANGHAI_TIMEZONE)

    def _is_expired(self, state: SyncedSheetState) -> bool:
        expires_at = parse_state_time(state.monitor_expires_at)
        return expires_at is not None and expires_at <= self._now()

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _migrate_legacy_hash(self) -> None:
        if not self._legacy_hash_key:
            return
        grouped: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        for sync_id, raw in self._redis.hgetall(self._legacy_hash_key).items():
            parsed = parse_sync_id(sync_id)
            if parsed is None:
                continue
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or not _string_value(value.get("target_url")):
                continue
            record_id, source_token, revision = parsed
            grouped.setdefault((record_id, source_token), []).append((revision, value))

        migrated = 0
        for (record_id, source_token), entries in grouped.items():
            entries.sort(key=lambda item: item[0])
            dated_entries = [
                (revision, value)
                for revision, value in entries
                if parse_state_time(_string_value(value.get("synced_at"))) is not None
            ]
            monitor_started_at = min(
                (_string_value(value.get("synced_at")) for _, value in dated_entries),
                key=lambda item: parse_state_time(item) or self._now(),
                default=format_state_time(self._now()),
            )
            monitor_expires_at = format_state_time(
                self._monitor_expiration(monitor_started_at)
            )
            if (parse_state_time(monitor_expires_at) or self._now()) <= self._now():
                continue

            versions: list[dict[str, Any]] = []
            for index, (revision, value) in enumerate(entries, start=1):
                versions.append(
                    {
                        "revision": revision,
                        "copy_version": _positive_int(value.get("copy_version")) or index,
                        "target_name": _string_value(value.get("target_name")),
                        "target_url": _string_value(value.get("target_url")),
                        "synced_at": _string_value(value.get("synced_at")),
                    }
                )
            latest_revision, latest = entries[-1]
            latest_version = versions[-1]
            state = SyncedSheetState(
                record_id=record_id,
                source_token=source_token,
                synced_revision=latest_revision,
                source_name=_string_value(latest.get("source_name")),
                source_url=_string_value(latest.get("source_url")),
                record_url=_string_value(latest.get("record_url")),
                target_name=_string_value(latest_version.get("target_name")),
                target_url=_string_value(latest_version.get("target_url")),
                synced_at=_string_value(latest_version.get("synced_at")),
                copy_version=_positive_int(latest_version.get("copy_version")) or len(versions),
                monitor_started_at=monitor_started_at,
                monitor_expires_at=monitor_expires_at,
                pending_revision=_optional_int(latest.get("pending_revision")),
                pending_since=_string_value(latest.get("pending_since")),
                versions=tuple(versions),
            )
            if self._write_state(state, nx=True):
                migrated += 1
        if migrated:
            self._logger.info("旧 Redis Hash 迁移完成：migrated=%d", migrated)


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


def format_state_time(value: datetime) -> str:
    return value.astimezone(_SHANGHAI_TIMEZONE).isoformat(timespec="seconds")


def parse_state_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


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

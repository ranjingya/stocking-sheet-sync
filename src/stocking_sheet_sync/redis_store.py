from __future__ import annotations

import json
import logging
import math
import uuid
from typing import Any

from redis import Redis

from .models import SyncedRecord


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
        }
        self._redis.hset(
            self._synced_key,
            sync_id,
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        )

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

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, replace
from typing import Any
from urllib.parse import urlsplit

from redis import Redis

from .models import CopyResult, CopyState

_COMPARE_DELETE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_COMPARE_SET = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2])
    return 1
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
        """
        功能说明：连接 Redis，准备永久去重记录与并发锁存储。

        参数：
            redis_url：Redis 连接地址。
            key_prefix：当前业务的键命名空间。
            socket_timeout_seconds：连接和读写超时秒数。
            client：可选 Redis 客户端，用于注入隔离的测试实现。
            logger：可选日志记录器。

        返回值：无；连接失败时抛出异常。
        """
        self._logger = logger or logging.getLogger(__name__)
        self._key_prefix = key_prefix.rstrip(":")
        if not self._key_prefix:
            raise ValueError("Redis key_prefix 不能为空")
        self._redis = client or Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=socket_timeout_seconds,
            socket_connect_timeout=socket_timeout_seconds,
            health_check_interval=30,
        )
        self._redis.ping()
        self._logger.info("Redis 状态存储连接成功：key_prefix=%s", self._key_prefix)

    @property
    def _lock_key(self) -> str:
        return f"{self._key_prefix}:lock:scan"

    def _state_key(self, record_id: str, source_token: str) -> str:
        return f"{self._key_prefix}:{record_id}:{source_token}"

    def acquire_run_lock(self, ttl_seconds: int) -> str | None:
        """获取搬运锁；ttl_seconds 为有效秒数，返回锁凭证或 None。"""
        token = uuid.uuid4().hex
        return token if self._redis.set(self._lock_key, token, nx=True, ex=ttl_seconds) else None

    def release_run_lock(self, token: str) -> None:
        """释放属于 token 的搬运锁，无返回值。"""
        self._redis.eval(_COMPARE_DELETE, 1, self._lock_key, token)

    def get_state(self, record_id: str, source_token: str) -> CopyState | None:
        """读取 record_id 与 source_token 的去重状态，返回状态或 None；损坏时抛错。"""
        key = self._state_key(record_id, source_token)
        raw = self._redis.get(key)
        if raw is None:
            return None
        return self._decode_state(key, raw)

    def begin_copy(self, state: CopyState) -> bool:
        """
        功能说明：复制前原子写入永久占位，防止锁过期或进程退出造成重复复制。

        参数：
            state：含源记录信息、唯一 attempt_id 和 copying 状态的本次搬运记录。

        返回值：成功占位返回 True；已有记录时返回 False。
        """
        if state.status != "copying" or not state.attempt_id:
            raise ValueError("复制占位必须包含 copying 状态和 attempt_id")
        return bool(
            self._redis.set(
                self._state_key(state.record_id, state.source_token), _encode(state), nx=True
            )
        )

    def finish_copy(self, state: CopyState, result: CopyResult, copied_at: str) -> None:
        """
        功能说明：将本次占位原子替换为永久的成功搬运记录。

        参数：
            state：复制前成功写入的占位状态。
            result：飞书返回的目标副本信息。
            copied_at：确认复制成功的时间。

        返回值：无；占位不匹配时抛错，避免覆盖其他处理结果。
        """
        completed = replace(
            state,
            status="copied",
            target_token=result.token,
            target_name=result.name,
            target_url=result.url,
            copied_at=copied_at,
        )
        if not self._redis.eval(
            _COMPARE_SET,
            1,
            self._state_key(state.record_id, state.source_token),
            _encode(state),
            _encode(completed),
        ):
            raise RuntimeError("搬运占位已变化，无法保存副本结果，请人工核对")

    def cancel_copy(self, state: CopyState) -> None:
        """仅在复制被明确拒绝时删除 state 对应的本次占位，无返回值。"""
        self._redis.eval(
            _COMPARE_DELETE, 1, self._state_key(state.record_id, state.source_token), _encode(state)
        )

    def migrate_legacy_records(self) -> None:
        """
        功能说明：启动时将命名空间内仍存在的历史成功记录转换为永久去重状态。

        参数：无。

        返回值：无；损坏记录保留供人工核对，Redis 故障时抛错阻止服务启动。
        """
        converted = 0
        for key in self._redis.scan_iter(match=f"{self._key_prefix}:*"):
            if (
                key == self._lock_key
                or len(key.removeprefix(f"{self._key_prefix}:").split(":")) != 2
            ):
                continue
            raw = self._redis.get(key)
            if raw is None:
                continue
            try:
                state = self._decode_state(key, raw)
                desired = _encode(state)
            except (ValueError, TypeError, KeyError) as error:
                self._logger.error("去重状态无效，保留并阻止重复搬运：key=%s reason=%s", key, error)
                desired = raw
            converted += bool(self._redis.eval(_COMPARE_SET, 1, key, raw, desired))
        self._logger.info("永久去重记录加载完成：record_count=%d", converted)

    def _decode_state(self, key: str, raw: str) -> CopyState:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"去重记录不是对象：{key}")
        if "status" not in data:
            if not all(
                isinstance(data.get(name), str) and data[name]
                for name in ("record_id", "source_token", "target_name", "target_url", "synced_at")
            ):
                raise ValueError(f"历史搬运结果不完整：{key}")
            data = {
                "record_id": data["record_id"],
                "source_token": data["source_token"],
                "source_name": data.get("source_name", ""),
                "source_url": data.get("source_url", ""),
                "record_url": data.get("record_url", ""),
                "status": "copied",
                "target_token": urlsplit(data["target_url"]).path.rstrip("/").rsplit("/", 1)[-1],
                "target_name": data["target_name"],
                "target_url": data["target_url"],
                "copied_at": data["synced_at"],
            }
        state = CopyState(**data)
        if key != self._state_key(state.record_id, state.source_token):
            raise ValueError(f"去重记录身份与键不匹配：{key}")
        if state.status not in {"copying", "copied"}:
            raise ValueError(f"去重状态无效：{key}")
        if state.status == "copying" and not state.attempt_id:
            raise ValueError(f"搬运占位缺少凭证：{key}")
        if state.status == "copied" and not all(
            (state.target_token, state.target_url, state.copied_at)
        ):
            raise ValueError(f"搬运结果不完整：{key}")
        return state

    def close(self) -> None:
        self._redis.close()


def _encode(state: CopyState) -> str:
    return json.dumps(asdict(state), ensure_ascii=False, sort_keys=True)

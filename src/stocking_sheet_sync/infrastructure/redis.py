from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, replace
from typing import Any
from urllib.parse import urlsplit

from redis import Redis

from stocking_sheet_sync.domain.models import CopyResult, CopyState, CopyStep, FillState
from stocking_sheet_sync.infrastructure.lease import assert_run_lock

_COMPARE_EXPIRE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

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
        self._logger.debug("Redis 状态存储连接成功：key_prefix=%s", self._key_prefix)

    @property
    def _lock_key(self) -> str:
        return f"{self._key_prefix}:lock:scan"

    def _state_key(self, record_id: str, source_token: str, request_id: str = "") -> str:
        assert_run_lock()
        if request_id:
            return f"{self._key_prefix}:force:{record_id}:{request_id}"
        return f"{self._key_prefix}:{record_id}:{source_token}"

    def _copy_key(self, state: CopyState) -> str:
        return self._state_key(state.record_id, state.source_token, state.request_id)

    def acquire_run_lock(self, ttl_seconds: int) -> str | None:
        """获取搬运锁；ttl_seconds 为有效秒数，返回锁凭证或 None。"""
        token = uuid.uuid4().hex
        return token if self._redis.set(self._lock_key, token, nx=True, ex=ttl_seconds) else None

    def renew_run_lock(self, token: str, ttl_seconds: int) -> bool:
        """
        功能说明：原子核对所有权后刷新运行锁有效期，不重新获取已失效的锁。

        参数：
            token：本次任务获得的锁凭证。
            ttl_seconds：从续期时刻起计算的有效秒数。
        返回值：成功续期为True；锁已失效或属于其他任务时为False。
        """
        return bool(self._redis.eval(_COMPARE_EXPIRE, 1, self._lock_key, token, ttl_seconds))

    def release_run_lock(self, token: str) -> None:
        """释放属于 token 的搬运锁，无返回值。"""
        self._redis.eval(_COMPARE_DELETE, 1, self._lock_key, token)

    def get_state(
        self, record_id: str, source_token: str, *, request_id: str = ""
    ) -> CopyState | None:
        """
        功能说明：读取普通任务或指定强制批次，并核对源文件身份。

        参数：
            record_id：多维表记录 ID。
            source_token：本次解析的真实源电子表格 token。
            request_id：强制请求标识；为空时读取普通任务。
        返回值：任务状态或 None；同一强制请求绑定不同源文件时抛错。
        """
        key = self._state_key(record_id, source_token, request_id)
        raw = self._redis.get(key)
        if raw is None:
            return None
        state = self._decode_state(key, raw)
        if state.source_token != source_token:
            raise ValueError("该 request_id 已绑定另一份源表格，请使用新的 request_id")
        if "backup_name" not in json.loads(raw):
            # 为历史普通及强制批次补齐默认字段，使后续比较更新保持一致。
            if not self._redis.eval(_COMPARE_SET, 1, key, raw, _encode(state)):
                raise RuntimeError("历史批次状态已变化，请重试读取")
            self._logger.debug("历史批次命名字段已补齐：key=%s", key)
        return state

    def begin_copy(self, state: CopyState) -> bool:
        """
        功能说明：复制前原子写入永久占位，防止锁过期或进程退出造成重复复制。

        参数：
            state：含源记录信息、唯一 attempt_id 和 copying 状态的本次搬运记录。

        返回值：成功占位返回 True；已有记录时返回 False。
        """
        if state.status != "copying" or not state.attempt_id:
            raise ValueError("复制占位必须包含 copying 状态和 attempt_id")
        return bool(self._redis.set(self._copy_key(state), _encode(state), nx=True))

    def finish_copy(self, state: CopyState, result: CopyResult, copied_at: str) -> CopyState:
        """
        功能说明：将本次占位原子替换为永久的成功搬运记录。

        参数：
            state：复制前成功写入的占位状态。
            result：飞书返回的目标副本信息。
            copied_at：确认复制成功的时间。

        返回值：保存后的副本记录；占位不匹配时抛错，避免覆盖其他处理结果。
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
            self._copy_key(state),
            _encode(state),
            _encode(completed),
        ):
            raise RuntimeError("搬运占位已变化，无法保存副本结果，请人工核对")
        return completed

    def cancel_copy(self, state: CopyState) -> None:
        """仅在复制被明确拒绝时删除 state 对应的本次占位，无返回值。"""
        self._redis.eval(_COMPARE_DELETE, 1, self._copy_key(state), _encode(state))

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
                or key.startswith(f"{self._key_prefix}:queue:")
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
        self._logger.debug("永久去重记录加载完成：record_count=%d", converted)

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
        if key != self._copy_key(state):
            raise ValueError(f"去重记录身份与键不匹配：{key}")
        if state.workflow not in {"single", "triple"}:
            raise ValueError(f"批次工作流程无效：{key}")
        if state.workflow == "triple" and not all(
            (state.backup_folder_token, state.delivery_folder_token, state.target_name)
        ):
            raise ValueError(f"三份文件批次配置不完整：{key}")
        if state.status not in {"copying", "copied"}:
            raise ValueError(f"去重状态无效：{key}")
        if state.status == "copying" and not state.attempt_id:
            raise ValueError(f"搬运占位缺少凭证：{key}")
        if state.status == "copied" and not all(
            (state.target_token, state.target_url, state.copied_at)
        ):
            raise ValueError(f"搬运结果不完整：{key}")
        return state

    def get_fill(self, state: CopyState) -> FillState | None:
        """读取 state 对应的历史填充状态；身份或状态损坏时阻止写入。"""
        raw = self._redis.get(self._fill_key(state))
        if raw is None:
            return None
        result = FillState(**json.loads(raw))
        if (result.record_id, result.source_token, result.target_token) != (
            state.record_id,
            state.source_token,
            state.target_token,
        ) or result.status not in {"running", "completed", "retryable", "needs_review"}:
            raise ValueError("历史填充记录身份或状态无效")
        if any(
            type(value) is not bool
            for value in (
                result.history_enabled,
                result.forecast_enabled,
                result.new_history_enabled,
                result.new_forecast_enabled,
            )
        ):
            raise ValueError("填充记录开关无效")
        if not result.attempt_id or not result.as_of:
            raise ValueError("历史填充记录缺少执行凭证或预估日")
        return result

    def begin_fill(self, state: CopyState, claim: FillState) -> bool:
        """
        功能说明：永久占位历史填充，只允许未开始或明确可重试的记录进入执行。

        参数：
            state：已保存的副本记录。
            claim：包含固定预估日和唯一执行凭证的填充占位。
        返回值：成功接管返回 True；已有执行、完成或待人工核验状态返回 False。
        """
        if state.status != "copied" or claim.status != "running":
            raise ValueError("填充必须针对已确认副本并使用 running 占位")
        if (claim.record_id, claim.source_token, claim.target_token) != (
            state.record_id,
            state.source_token,
            state.target_token,
        ):
            raise ValueError("填充占位与副本身份不符")
        key = self._fill_key(state)
        raw = self._redis.get(key)
        if raw is None:
            return bool(self._redis.set(key, _encode(claim), nx=True))
        previous = self.get_fill(state)
        if previous.status != "retryable" or previous.as_of != claim.as_of:
            return False
        return bool(self._redis.eval(_COMPARE_SET, 1, key, raw, _encode(claim)))

    def finish_fill(self, state: CopyState, claim: FillState, result: FillState) -> None:
        """将 state 的 claim 原子更新为 result；占位变化时抛错，避免覆盖另一执行。"""
        if (
            replace(
                result,
                status=claim.status,
                reason=claim.reason,
                history_status=claim.history_status,
                forecast_status=claim.forecast_status,
            )
            != claim
        ):
            raise ValueError("填充结束状态不能改变执行身份、日期或报告路径")
        if result.status == "running":
            raise ValueError("填充结束状态不能为 running")
        if not self._redis.eval(
            _COMPARE_SET, 1, self._fill_key(state), _encode(claim), _encode(result)
        ):
            raise RuntimeError("填充占位已变化，请保留副本并核对执行报告")

    def _fill_key(self, state: CopyState) -> str:
        return self._copy_key(state) + ":history"

    def get_step(self, state: CopyState, stage: str) -> CopyStep | None:
        """读取 state 的 stage 复制步骤，校验阶段、状态和成功结果后返回。"""
        raw = self._redis.get(self._step_key(state, stage))
        if raw is None:
            return None
        step = CopyStep(**json.loads(raw))
        if step.stage != stage or step.status not in {"copying", "copied"} or not step.attempt_id:
            raise ValueError("复制步骤记录无效")
        if step.status == "copied" and not all(
            (step.target_token, step.target_url, step.copied_at)
        ):
            raise ValueError("复制步骤成功记录不完整")
        return step

    def begin_step(self, state: CopyState, step: CopyStep) -> bool:
        """为 state 的 step 创建永久占位；已有步骤时返回 False。"""
        if step.status != "copying" or not step.attempt_id:
            raise ValueError("复制步骤必须以有效占位开始")
        return bool(self._redis.set(self._step_key(state, step.stage), _encode(step), nx=True))

    def finish_step(
        self, state: CopyState, step: CopyStep, result: CopyResult, at: str
    ) -> CopyStep:
        """
        功能说明：将阶段占位原子替换为复制结果，防止重试重复创建文件。

        参数：
            state：所属批次。
            step：本次已占位的步骤。
            result：服务端返回的副本信息。
            at：确认复制成功的时间。
        返回值：完成的步骤；占位变化时抛错，不重试复制。
        """
        completed = replace(
            step,
            status="copied",
            target_token=result.token,
            target_url=result.url,
            name=result.name,
            copied_at=at,
        )
        if not self._redis.eval(
            _COMPARE_SET, 1, self._step_key(state, step.stage), _encode(step), _encode(completed)
        ):
            raise RuntimeError("复制步骤占位变化，请核对已创建文件")
        return completed

    def finish_step_rename(self, state: CopyState, step: CopyStep, name: str) -> CopyStep:
        """
        功能说明：原子保存已核验的阶段文件名，保留原复制身份及去重记录。

        参数：
            state：所属批次。
            step：改名前的已完成复制步骤。
            name：服务端已核验的标题。
        返回值：更新后的步骤；状态变化时抛错，供同批次重试重新核对。
        """
        if step.status != "copied" or not name:
            raise ValueError("只能更新已完成复制步骤的有效名称")
        updated = replace(step, name=name)
        if not self._redis.eval(
            _COMPARE_SET, 1, self._step_key(state, step.stage), _encode(step), _encode(updated)
        ):
            raise RuntimeError("备份改名状态保存失败，请重试同一批次核对")
        return updated

    def cancel_step(self, state: CopyState, step: CopyStep) -> None:
        """只在明确拒绝复制时删除 state 的 step 占位。"""
        self._redis.eval(_COMPARE_DELETE, 1, self._step_key(state, step.stage), _encode(step))

    def get_outcome(self, state: CopyState) -> dict | None:
        """读取 state 已冻结的填充结果及交付来源；返回 None 或结果字典。"""
        raw = self._redis.get(self._copy_key(state) + ":outcome")
        if raw is None:
            return None
        result = json.loads(raw)
        required = {
            "history_status",
            "forecast_status",
            "fill_degraded",
            "reason",
            "fill_report_path",
            "delivery_source",
            "source_token",
        }
        if (
            not isinstance(result, dict)
            or not required <= result.keys()
            or result["delivery_source"] not in {"original", "filled"}
        ):
            raise ValueError("交付决策记录无效")
        return result

    def save_outcome(self, state: CopyState, outcome: dict) -> dict:
        """首次保存 state 的 outcome，后续读取已冻结的决定，避免重试更换交付内容。"""
        self._redis.set(
            self._copy_key(state) + ":outcome",
            json.dumps(outcome, ensure_ascii=False, sort_keys=True),
            nx=True,
        )
        return self.get_outcome(state)

    def _step_key(self, state: CopyState, stage: str) -> str:
        if stage not in {"original", "filled", "delivery"}:
            raise ValueError("复制步骤名称无效")
        return self._copy_key(state) + ":step:" + stage

    def close(self) -> None:
        self._redis.close()


def _encode(state: CopyState | FillState | CopyStep) -> str:
    return json.dumps(asdict(state), ensure_ascii=False, sort_keys=True)

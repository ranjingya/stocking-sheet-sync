"""Redis Streams任务队列；只删除已确认完成的消息。"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from redis import Redis
from redis.exceptions import ResponseError

from stocking_sheet_sync.infrastructure.lease import assert_run_lock

LOG = logging.getLogger(__name__)
_ENQUEUE = """
local id = redis.call('XADD', KEYS[1], '*', 'record_id', ARGV[1])
redis.call('HSET', ARGV[2] .. id, 'record_id', ARGV[1],
    'status', 'queued', 'attempts', '0', 'updated_at', ARGV[3])
return id
"""


@dataclass(frozen=True)
class QueuedTask:
    task_id: str
    record_id: str


class TaskQueue:
    def __init__(self, config, *, client=None):
        """
        功能说明：连接Redis队列并创建消费组，队列消息与业务去重分别存储。

        参数：
            config：包含Redis连接、命名空间和任务结果保留时长的配置。
            client：可选测试Redis客户端。
        返回值：无。
        """
        self.redis = client or Redis.from_url(
            config.redis_url,
            decode_responses=True,
            socket_timeout=max(5, config.request_timeout_seconds),
            socket_connect_timeout=config.request_timeout_seconds,
        )
        self.stream = config.redis_key_prefix.rstrip(":") + ":queue:tasks"
        self.status_prefix = config.redis_key_prefix.rstrip(":") + ":queue:result:"
        self.group = "sync"
        self.consumer = "serial-worker"
        self.retention = config.queue_result_ttl_seconds
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP"):
                self.redis.close()
                raise

    def close(self):
        """关闭队列连接。"""
        self.redis.close()

    def enqueue(self, record_id: str) -> str:
        """原子保存record_id的队列消息与初始状态，返回任务ID。"""
        task_id = self.redis.eval(
            _ENQUEUE, 1, self.stream, record_id, self.status_prefix, self._now()
        )
        LOG.info("任务已入队：task_id=%s record_id=%s", task_id, record_id)
        return task_id

    def take(self) -> QueuedTask | None:
        """优先恢复固定消费者未确认的任务，再等待新任务；调用方必须独占Worker锁。"""
        assert_run_lock()
        pending = self.redis.xreadgroup(self.group, self.consumer, {self.stream: "0"}, count=1)
        if not pending or not pending[0][1]:
            pending = self.redis.xreadgroup(
                self.group, self.consumer, {self.stream: ">"}, count=1, block=1000
            )
        if not pending or not pending[0][1]:
            return None
        task_id, fields = pending[0][1][0]
        return QueuedTask(task_id, fields.get("record_id", ""))

    def status(self, task_id: str) -> dict:
        """读取task_id对应的执行状态；已过期或不存在时返回空字典。"""
        return self.redis.hgetall(self.status_prefix + task_id)

    def begin(self, task: QueuedTask) -> int:
        """记录task开始执行并增加尝试次数，返回累计次数。"""
        assert_run_lock()
        with self.redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(self.status_prefix + task.task_id, "attempts", 1)
            pipe.hset(
                self.status_prefix + task.task_id,
                mapping={"status": "running", "updated_at": self._now()},
            )
            return int(pipe.execute()[0])

    def defer(self, task: QueuedTask, reason: str, *, busy: bool = False):
        """保留task在未确认队列中等待重试；busy表示锁忙，不消耗执行次数。"""
        assert_run_lock()
        with self.redis.pipeline(transaction=True) as pipe:
            if busy:
                pipe.hincrby(self.status_prefix + task.task_id, "attempts", -1)
            pipe.hset(
                self.status_prefix + task.task_id,
                mapping={"status": "retrying", "reason": reason, "updated_at": self._now()},
            )
            pipe.execute()

    def finish(self, task: QueuedTask, status: str, result: dict):
        """原子保存task的最终status和result、确认消费并移除消息，结果按配置保留。"""
        assert_run_lock()
        with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self.status_prefix + task.task_id,
                mapping={
                    "status": status,
                    "result": json.dumps(result, ensure_ascii=False),
                    "updated_at": self._now(),
                },
            )
            pipe.expire(self.status_prefix + task.task_id, self.retention)
            pipe.xack(self.stream, self.group, task.task_id)
            pipe.xdel(self.stream, task.task_id)
            pipe.execute()
        LOG.info("队列任务完成：task_id=%s status=%s", task.task_id, status)

    @staticmethod
    def _now():
        return datetime.now(UTC).isoformat()

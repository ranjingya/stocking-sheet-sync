"""运行锁续期与当前任务的锁所有权检查。"""

import logging
from contextvars import ContextVar
from threading import Event, Thread
from time import monotonic

LOG = logging.getLogger(__name__)
_CURRENT = ContextVar("run_lease", default=None)


def assert_run_lock() -> None:
    """检查当前任务的锁是否仍然有效；独立诊断命令不受影响。"""
    lease = _CURRENT.get()
    if lease is not None and monotonic() >= lease.deadline:
        lease.lost.set()
    if lease is not None and lease.lost.is_set():
        raise RuntimeError("运行锁续期失败或所有权丢失，停止后续操作，请核对本批次状态")


class RunLease:
    def __init__(self, store, token: str, ttl_seconds: int):
        """
        功能说明：维护已获取运行锁的后台续期与失效状态。

        参数：
            store：支持按凭证续期的Redis状态存储。
            token：当前任务取得的锁凭证。
            ttl_seconds：锁有效期，续期间隔为其三分之一。
        返回值：无。
        """
        self.store = store
        self.token = token
        self.ttl = ttl_seconds
        self.deadline = monotonic() + ttl_seconds
        self.stop = Event()
        self.lost = Event()
        self.thread = Thread(target=self._run, name="stocking-lock-renewal", daemon=True)
        self.context_token = None

    def start(self) -> None:
        """绑定当前任务并启动续期线程。"""
        self.context_token = _CURRENT.set(self)
        try:
            self.thread.start()
        except Exception:
            _CURRENT.reset(self.context_token)
            self.context_token = None
            raise
        LOG.info("运行锁自动续期开始：ttl=%s interval=%s", self.ttl, self.ttl / 3)

    def renew(self) -> bool:
        """续期一次；失败后标记失效，禁止重新获取或覆盖其他任务的锁。"""
        started = monotonic()
        if self.lost.is_set() or started >= self.deadline:
            self.lost.set()
            return False
        try:
            if self.store.renew_run_lock(self.token, self.ttl):
                if monotonic() >= self.deadline or self.lost.is_set():
                    self.lost.set()
                    return False
                self.deadline = started + self.ttl
                LOG.debug("运行锁续期完成")
                return True
            LOG.error("运行锁所有权已丢失，停止后续外部操作")
        except Exception:
            LOG.exception("运行锁续期异常，停止后续外部操作")
        self.lost.set()
        return False

    def _run(self) -> None:
        """等待续期间隔并刷新锁，任务结束或续期失败即退出。"""
        while not self.stop.wait(self.ttl / 3):
            if not self.renew():
                return

    def close(self) -> None:
        """停止并回收续期线程，恢复调用方上下文。"""
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join()
        if self.context_token is not None:
            _CURRENT.reset(self.context_token)
        LOG.info("运行锁自动续期结束：lost=%s", self.lost.is_set())

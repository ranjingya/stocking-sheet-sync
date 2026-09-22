from threading import Event

import pytest

from stocking_sheet_sync.infrastructure.lease import RunLease, assert_run_lock
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from tests.fakes import FakeRedis


def test_renew_only_owned_existing_lock():
    redis = FakeRedis()
    store = RedisStateStore("redis://unused", "lease-test", client=redis)
    token = store.acquire_run_lock(3)
    assert store.renew_run_lock(token, 9)
    assert redis.expirations[store._lock_key] == 9
    assert not store.renew_run_lock("other", 30)
    assert redis.expirations[store._lock_key] == 9
    redis.delete(store._lock_key)
    assert not store.renew_run_lock(token, 30)
    other = store.acquire_run_lock(3)
    assert not store.renew_run_lock(token, 30)
    store.release_run_lock(token)
    assert redis.get(store._lock_key) == other


def test_background_renewal_and_close():
    renewed = Event()

    class Store:
        def renew_run_lock(self, token, ttl):
            assert token == "owned" and ttl == 0.03
            renewed.set()
            return True

    lease = RunLease(Store(), "owned", 0.03)
    lease.start()
    try:
        assert renewed.wait(1)
        assert_run_lock()
    finally:
        lease.close()
    assert not lease.thread.is_alive()
    assert_run_lock()


@pytest.mark.parametrize("raises", [False, True])
def test_loss_blocks_external_work_and_restores_context(raises):
    class Store:
        def renew_run_lock(self, *args):
            if raises:
                raise ConnectionError("Redis暂不可用")
            return False

    lease = RunLease(Store(), "owned", 300)
    lease.start()
    try:
        assert not lease.renew()
        with pytest.raises(RuntimeError, match="运行锁"):
            assert_run_lock()
    finally:
        lease.close()
    assert_run_lock()


def test_expired_local_deadline_stops_work_even_before_renewal_thread():
    from unittest.mock import Mock

    store = Mock()
    lease = RunLease(store, "owned", 300)
    lease.start()
    try:
        lease.deadline = 0
        with pytest.raises(RuntimeError, match="运行锁"):
            assert_run_lock()
        assert not lease.renew()
        store.renew_run_lock.assert_not_called()
    finally:
        lease.close()

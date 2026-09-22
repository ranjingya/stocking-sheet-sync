from dataclasses import replace

import pytest

from stocking_sheet_sync.domain.models import CopyResult, CopyState
from stocking_sheet_sync.infrastructure.redis import RedisStateStore
from tests.fakes import FakeRedis


def fixture_store():
    redis = FakeRedis()
    store = RedisStateStore("redis://unused", "test", client=redis)
    state = CopyState(
        "rec_a",
        "source",
        "名称",
        "https://example.feishu.cn/sheets/source",
        "https://example.feishu.cn/record/a",
        "copying",
        attempt_id="attempt-1",
    )
    return store, redis, state


def test_lock_release_checks_owner():
    store, redis, _ = fixture_store()
    first = store.acquire_run_lock(30)
    assert redis.expirations["test:lock:scan"] == 30
    assert store.acquire_run_lock(30) is None
    redis.delete("test:lock:scan")
    second = store.acquire_run_lock(30)
    store.release_run_lock(first)
    assert redis.get("test:lock:scan") == second
    store.release_run_lock(second)
    assert redis.get("test:lock:scan") is None


def test_claims_are_permanent_and_compare_ownership():
    store, redis, state = fixture_store()
    assert store.begin_copy(state)
    assert not store.begin_copy(replace(state, attempt_id="attempt-2"))
    store.cancel_copy(replace(state, attempt_id="attempt-2"))
    result = CopyResult("目标", "target", "sheet", "https://example.feishu.cn/sheets/target")
    with pytest.raises(RuntimeError, match="占位已变化"):
        store.finish_copy(replace(state, attempt_id="attempt-2"), result, "today")
    store.finish_copy(state, result, "2026-09-14T10:00:00+08:00")
    store.cancel_copy(state)
    saved = store.get_state(state.record_id, state.source_token)
    assert saved.status == "copied"
    assert saved.target_token == "target"
    assert redis.expirations == {}


def test_startup_preserves_current_claim_and_lock():
    store, redis, state = fixture_store()
    store.begin_copy(state)
    lock = store.acquire_run_lock(30)
    store.migrate_legacy_records()
    assert store.get_state(state.record_id, state.source_token) == state
    assert redis.expirations == {"test:lock:scan": 30}
    assert redis.get("test:lock:scan") == lock


def test_invalid_history_is_retained_without_expiry():
    store, redis, state = fixture_store()
    redis.set("test:rec_a:source", "{}", ex=10)
    store.migrate_legacy_records()
    with pytest.raises(ValueError, match="不完整"):
        store.get_state(state.record_id, state.source_token)
    assert redis.get("test:rec_a:source") == "{}"
    assert not redis.expirations


def test_migration_does_not_read_queue_stream_as_string(monkeypatch):
    store, redis, _ = fixture_store()
    redis.strings["test:queue:tasks"] = "stream"
    redis.strings["test:queue:result:1-0"] = "hash"
    redis.strings["test:queue:worker:lock:scan"] = "lock"

    def reject_read(key):
        raise AssertionError(f"不应读取队列命名空间：{key}")

    monkeypatch.setattr(redis, "get", reject_read)
    store.migrate_legacy_records()

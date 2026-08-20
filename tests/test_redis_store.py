from datetime import UTC, datetime

from stocking_sheet_sync.models import SyncState
from stocking_sheet_sync.redis_store import RedisStateStore
from tests.fakes import FakeRedis


def test_redis_store_saves_human_readable_hash() -> None:
    client = FakeRedis()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "stocking-sheet-sync-test",
        client=client,
    )
    state = SyncState(
        record_id="rec_test",
        source_token="source-token",
        source_revision=12,
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/rec_test",
        target_token="target-token",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        copied_at="2026-08-20T14:00:00+08:00",
        status="success",
        pending_notify_open_ids=["ou_test"],
        updated_at=datetime.now(UTC).isoformat(),
    )

    store.save(state)
    restored = store.get("rec_test")

    assert restored == state
    redis_hash = client.hashes["stocking-sheet-sync-test:state:rec_test"]
    assert redis_hash["original_name"] == "备货测试表"
    assert redis_hash["record_url"].endswith("rec_test")
    assert redis_hash["target_url"].endswith("target-token")


def test_redis_lock_is_released_only_by_owner() -> None:
    client = FakeRedis()
    first = RedisStateStore("redis://localhost:6379/0", "sync", client=client)
    second = RedisStateStore("redis://localhost:6379/0", "sync", client=client)

    assert first.acquire_run_lock(1) is True
    assert second.acquire_run_lock(1) is False
    second.release_run_lock()
    assert "sync:lock:scan" in client.strings
    first.release_run_lock()
    assert second.acquire_run_lock(1) is True

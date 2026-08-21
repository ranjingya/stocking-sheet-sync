import json

from stocking_sheet_sync.models import SyncedRecord
from stocking_sheet_sync.redis_store import RedisStateStore
from tests.fakes import FakeRedis


def test_redis_store_saves_human_readable_hash() -> None:
    client = FakeRedis()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "stocking-sheet-sync-test",
        client=client,
    )
    record = SyncedRecord(
        record_id="rec_test",
        source_token="source-token",
        source_revision=12,
        source_name="备货测试表",
        source_url="https://example.feishu.cn/sheets/source-token",
        record_url="https://example.feishu.cn/record/rec_test",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        synced_at="2026-08-20T14:00:00+08:00",
    )

    store.save_synced(record)
    restored = store.get_synced("rec_test", "source-token", 12)

    assert store.is_synced("rec_test", "source-token", 12) is True
    assert store.is_synced("rec_test", "source-token", 13) is False
    assert restored is not None
    assert restored["source_name"] == "备货测试表"
    redis_hash = client.hashes["stocking-sheet-sync-test:synced"]
    raw = redis_hash["rec_test:source-token:12"]
    saved_value = json.loads(raw)
    assert "target_token" not in saved_value
    assert saved_value["copy_version"] == 1
    assert store.next_copy_version("rec_test", "source-token") == 2


def test_redis_store_groups_versions_and_persists_pending_state() -> None:
    client = FakeRedis()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "stocking-sheet-sync-test",
        client=client,
    )
    for revision in (4, 7):
        store.save_synced(
            SyncedRecord(
                record_id="rec_test",
                source_token="source-token",
                source_revision=revision,
                source_name="备货测试表",
                source_url="https://example.feishu.cn/sheets/source-token",
                record_url="https://example.feishu.cn/record/rec_test",
                target_name="市场部-备货测试表",
                target_url=f"https://example.feishu.cn/sheets/target-{revision}",
                synced_at="2026-08-21T10:00:00+08:00",
                copy_version=1 if revision == 4 else 2,
            )
        )

    latest = store.list_latest_synced()
    assert len(latest) == 1
    assert latest[0].synced_revision == 7
    assert store.next_copy_version("rec_test", "source-token") == 3

    store.save_pending(latest[0], 8, "2026-08-21T10:05:00+08:00")
    observed = store.list_latest_synced()[0]
    assert observed.pending_revision == 8
    assert observed.pending_since == "2026-08-21T10:05:00+08:00"

    store.save_pending(observed, None)
    cleared = store.list_latest_synced()[0]
    assert cleared.pending_revision is None
    assert cleared.pending_since == ""


def test_redis_store_derives_next_copy_version_from_legacy_records() -> None:
    client = FakeRedis()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "stocking-sheet-sync-test",
        client=client,
    )
    redis_hash = client.hashes.setdefault("stocking-sheet-sync-test:synced", {})
    for revision in (4, 7):
        redis_hash[f"rec_test:source-token:{revision}"] = json.dumps(
            {
                "target_url": f"https://example.feishu.cn/sheets/target-{revision}",
                "synced_at": "2026-08-21T10:00:00+08:00",
            }
        )

    assert store.next_copy_version("rec_test", "source-token") == 3


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

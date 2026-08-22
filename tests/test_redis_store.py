import json
from datetime import UTC, datetime, timedelta

from stocking_sheet_sync.models import SyncedRecord
from stocking_sheet_sync.redis_store import RedisStateStore
from tests.fakes import FakeRedis


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 21, 2, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def make_record(revision: int, copy_version: int = 1) -> SyncedRecord:
    return SyncedRecord(
        record_id="rec_test",
        source_token="source-token",
        source_revision=revision,
        source_name="备货测试表",
        source_url="https://example.feishu.cn/sheets/source-token",
        record_url="https://example.feishu.cn/record/rec_test",
        target_name=f"市场部-备货测试表-v{copy_version}",
        target_url=f"https://example.feishu.cn/sheets/target-{revision}",
        synced_at="2026-08-21T10:00:00+08:00",
        copy_version=copy_version,
    )


def test_redis_store_saves_one_string_with_complete_versions() -> None:
    client = FakeRedis()
    clock = MutableClock()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "ss",
        client=client,
        now_provider=clock,
    )

    store.save_synced(make_record(12))
    state = store.get_state("rec_test", "source-token")

    assert state is not None
    assert state.source_name == "备货测试表"
    assert state.synced_revision == 12
    assert len(state.versions) == 1
    assert store.is_synced("rec_test", "source-token", 12) is True
    assert store.is_synced("rec_test", "source-token", 13) is False
    assert store.next_copy_version("rec_test", "source-token") == 2
    raw = json.loads(client.strings["ss:rec_test:source-token"])
    assert raw["copy_version"] == 1
    assert raw["versions"][0]["revision"] == 12
    assert "ss:rec_test:source-token" in client.expirations


def test_redis_store_appends_versions_and_persists_pending_state() -> None:
    client = FakeRedis()
    clock = MutableClock()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "ss",
        client=client,
        now_provider=clock,
    )
    store.save_synced(make_record(4, 1))
    store.save_synced(make_record(7, 2))

    latest = store.list_latest_synced()
    assert len(latest) == 1
    assert latest[0].synced_revision == 7
    assert len(latest[0].versions) == 2
    assert store.next_copy_version("rec_test", "source-token") == 3

    store.save_pending(latest[0], 8, "2026-08-21T10:05:00+08:00")
    observed = store.list_latest_synced()[0]
    assert observed.pending_revision == 8
    assert observed.pending_since == "2026-08-21T10:05:00+08:00"

    store.save_pending(observed, None)
    cleared = store.list_latest_synced()[0]
    assert cleared.pending_revision is None
    assert cleared.pending_since == ""


def test_redis_store_expires_after_three_natural_days_without_extension() -> None:
    client = FakeRedis()
    clock = MutableClock()
    store = RedisStateStore(
        "redis://localhost:6379/0",
        "ss",
        monitor_days=3,
        client=client,
        now_provider=clock,
    )
    store.save_synced(make_record(4, 1))
    first_expiration = client.expirations["ss:rec_test:source-token"]

    clock.value += timedelta(days=1)
    store.save_synced(make_record(7, 2))
    assert client.expirations["ss:rec_test:source-token"] == first_expiration

    clock.value = datetime(2026, 8, 23, 16, 0, tzinfo=UTC)
    assert store.get_state("rec_test", "source-token") is None
    assert "ss:rec_test:source-token" not in client.strings


def test_redis_lock_is_released_only_by_owner() -> None:
    client = FakeRedis()
    store = RedisStateStore("redis://localhost:6379/0", "ss", client=client)

    owner_token = store.acquire_run_lock(1)
    assert isinstance(owner_token, str)
    assert store.acquire_run_lock(1) is None
    store.release_run_lock("not-the-owner")
    assert "ss:lock:scan" in client.strings
    store.release_run_lock(owner_token)
    assert isinstance(store.acquire_run_lock(1), str)

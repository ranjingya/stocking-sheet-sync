from dataclasses import replace

import pytest

from stocking_sheet_sync.domain.models import CopyResult
from stocking_sheet_sync.infrastructure.feishu.client import CopyRejected
from tests.services.test_sync_service import make_service


def setup(tmp_path, *, history=True, forecast=False):
    service, client, redis, clock = make_service(tmp_path)
    service.config = replace(
        service.config,
        backup_folder_token="backup",
        target_folder_token="delivery",
        fill_history_enabled=history,
        fill_forecast_enabled=forecast,
    )
    operations = []
    files = {"source-token": {"original": 1}}

    def copy(source, name, *, folder_token=None):
        client.copy_count += 1
        token = f"copy-{client.copy_count}"
        operations.append((source, folder_token, token, name))
        files[token] = dict(files[source])
        return CopyResult(name, token, "sheet", f"https://example.feishu.cn/sheets/{token}")

    client.copy_spreadsheet = copy
    client.renames = []
    client.rename_spreadsheet = lambda token, title: client.renames.append((token, title))
    calls = []

    def fill(state, claim):
        calls.append(state.target_token)
        files[state.target_token]["sales"] = 10
        return {"status": "completed"}

    service.history_filler = fill
    service.forecast_filler = fill
    return service, client, redis, clock, operations, files, calls


def test_three_files_preserve_original_and_deliver_identical_filled_copy(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    first = service.run_record("rec_test")
    assert first.result == "copied" and first.copied == 1
    assert [(o[0], o[1]) for o in ops] == [
        ("source-token", "backup"),
        ("copy-1", "backup"),
        ("copy-2", "delivery"),
    ]
    assert files["copy-1"] == files["source-token"] == {"original": 1}
    assert files["copy-2"] == files["copy-3"] == {"original": 1, "sales": 10}
    assert first.original_backup_url.endswith("copy-1")
    assert first.filled_backup_url.endswith("copy-2")
    assert first.target_url.endswith("copy-3") and first.delivery_source == "filled"
    files["copy-3"]["manual"] = 99
    saved = dict(redis.strings)
    service.config = replace(
        service.config,
        backup_folder_token="changed",
        target_folder_token="changed",
        fill_history_enabled=False,
    )
    again = service.run_record("rec_test")
    assert again.result == "unchanged" and again.target_url == first.target_url
    assert redis.strings == saved and len(ops) == 3 and fills == ["copy-2"]
    assert files["copy-3"]["manual"] == 99


@pytest.mark.parametrize("status", ["needs_review", "retryable", "exception"])
def test_fill_failure_delivers_original_and_freezes_fallback(tmp_path, status):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)

    def fail(state, claim):
        files[state.target_token]["partial"] = True
        if status == "exception":
            raise RuntimeError("部分写入失败")
        return {"status": status, "reason": "填充未完成"}

    service.history_filler = fail
    result = service.run_record("rec_test")
    assert result.result == "copied" and result.fill_degraded and result.failed == 0
    assert result.delivery_source == "original" and ops[-1][0] == "copy-1"
    assert files["copy-3"] == files["copy-1"] == {"original": 1}
    assert files["copy-2"]["partial"]
    service.history_filler = lambda *a: pytest.fail("已交付后不能重填")
    assert service.run_record("rec_test").result == "unchanged"
    assert len(ops) == 3


@pytest.mark.parametrize("failed_stage", [1, 2, 3])
def test_known_copy_rejection_only_retries_incomplete_stage(tmp_path, failed_stage):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    actual = client.copy_spreadsheet
    rejected = False

    def copy(*a, **k):
        nonlocal rejected
        if len(ops) + 1 == failed_stage and not rejected:
            rejected = True
            raise CopyRejected("明确拒绝")
        return actual(*a, **k)

    client.copy_spreadsheet = copy
    assert service.run_record("rec_test").failed == 1
    assert len(ops) == failed_stage - 1
    assert service.run_record("rec_test").result == "copied"
    assert len(ops) == 3 and fills == ["copy-2"]


def test_delivery_rejection_does_not_retry_filling_or_change_selected_source(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    service.history_filler = lambda *a: {"status": "retryable", "reason": "缺数据"}
    actual = client.copy_spreadsheet

    def copy(*a, **k):
        if len(ops) == 2:
            raise CopyRejected("交付被拒绝")
        return actual(*a, **k)

    client.copy_spreadsheet = copy
    assert service.run_record("rec_test").failed == 1
    service.history_filler = lambda *a: pytest.fail("已固定交付内容，不应再次填充")
    client.copy_spreadsheet = actual
    result = service.run_record("rec_test")
    assert result.fill_degraded and result.delivery_source == "original"
    assert ops[-1][0] == "copy-1"


@pytest.mark.parametrize("failed_stage", [1, 2, 3])
def test_unknown_copy_result_blocks_duplicate_even_after_lock_expiry(tmp_path, failed_stage):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    actual = client.copy_spreadsheet

    def copy(*a, **k):
        result = actual(*a, **k)
        if len(ops) == failed_stage:
            redis.delete("test:lock:scan")
            assert service.run_record("rec_test").failed == 1
            raise RuntimeError("响应丢失")
        return result

    client.copy_spreadsheet = copy
    assert service.run_record("rec_test").failed == 1
    assert service.run_record("rec_test").failed == 1
    assert len(ops) == failed_stage


def test_concurrent_fill_cannot_deliver_while_first_run_is_writing(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)

    def fill(state, claim):
        redis.delete("test:lock:scan")
        assert service.run_record("rec_test").failed == 1
        assert len(ops) == 2
        return {"status": "completed"}

    service.history_filler = fill
    assert service.run_record("rec_test").result == "copied"
    assert len(ops) == 3


def test_delivery_completed_but_batch_save_failed_does_not_recopy(tmp_path, monkeypatch):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    actual = service.store.finish_copy
    monkeypatch.setattr(
        service.store, "finish_copy", lambda *a: (_ for _ in ()).throw(RuntimeError("状态保存失败"))
    )
    assert service.run_record("rec_test").failed == 1
    monkeypatch.setattr(service.store, "finish_copy", actual)
    assert service.run_record("rec_test").result == "copied"
    assert len(ops) == 3 and fills == ["copy-2"]


def test_force_creates_another_three_file_batch(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    assert service.run_record("rec_test").copied == 1
    assert service.run_record("rec_test", force=True, request_id="new").copied == 1
    assert service.run_record("rec_test", force=True, request_id="new").unchanged == 1
    assert len(ops) == 6 and fills == ["copy-2", "copy-5"]


def test_disabled_fill_still_makes_three_copies(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path, history=False)
    assert service.run_record("rec_test").result == "copied"
    assert len(ops) == 3 and not fills
    assert files["copy-1"] == files["copy-2"] == files["copy-3"]


def test_existing_single_copy_is_reused_after_enabling_backups(tmp_path):
    service, client, redis, clock = make_service(tmp_path)
    original = service.run_record("rec_test")
    service.config = replace(service.config, backup_folder_token="backup")
    assert service.run_record("rec_test").target_url == original.target_url
    assert client.copy_count == 1


@pytest.mark.parametrize("suffix", [":step:original", ":step:filled", ":step:delivery", ":outcome"])
def test_completed_batch_missing_stage_never_creates_more_files(tmp_path, suffix):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    assert service.run_record("rec_test").copied == 1
    redis.delete("test:rec_test:source-token" + suffix)
    assert service.run_record("rec_test").failed == 1
    assert len(ops) == 3 and fills == ["copy-2"]


def test_fill_state_commit_failure_prevents_premature_delivery(tmp_path, monkeypatch):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    monkeypatch.setattr(
        service.store, "finish_fill", lambda *a: (_ for _ in ()).throw(RuntimeError("状态保存失败"))
    )
    assert service.run_record("rec_test").failed == 1
    assert service.run_record("rec_test").failed == 1
    assert len(ops) == 2 and fills == ["copy-2"]


def test_notification_contains_all_three_links_and_delivery_source(tmp_path):
    import json

    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    service.history_filler = lambda *a: {"status": "needs_review", "reason": "填充失败"}
    result = service.run_record("rec_test")
    card = json.dumps(client.sent_cards[-1], ensure_ascii=False)
    assert all(
        link in card
        for link in [result.original_backup_url, result.filled_backup_url, result.target_url]
    )
    assert "交付内容：原始备份" in card and "填充未完成" in card


def test_forecast_failure_delivers_original_and_preserves_processing_copy(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path, forecast=True)

    def fail(state, claim):
        files[state.target_token]["partial"] = 10
        return {"status": "needs_review", "reason": "缺少历史数据"}

    service.forecast_filler = fail
    result = service.run_record("rec_test")
    assert result.history_status == result.forecast_status == "needs_review"
    assert result.fill_degraded and result.delivery_source == "original"
    assert files["copy-3"] == files["source-token"] and "partial" in files["copy-2"]


def test_names_freeze_timestamp_and_keep_delivery_prefix(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    assert service.run_record("rec_test").copied == 1
    assert [op[3] for op in ops] == [
        "备货测试表-20260821-100000-原始备份",
        "备货测试表-20260821-100000-填充未完成",
        "市场部-备货测试表",
    ]
    assert client.renames == [("copy-2", "备货测试表-20260821-100000-填充完成")]
    state = service.store.get_state("rec_test", "source-token")
    assert service.store.get_step(state, "filled").name == client.renames[0][1]
    clock.advance(minutes=10)
    assert service.run_record("rec_test").unchanged == 1
    assert len(client.renames) == 1
    assert service.run_record("rec_test", force=True, request_id="new").copied == 1
    assert ops[3][3] == "备货测试表-20260821-101000-原始备份"
    assert ops[2][3] == ops[5][3] == "市场部-备货测试表"


@pytest.mark.parametrize("mode", ["failure", "disabled"])
def test_backup_title_reflects_failure_or_disabled_fill(tmp_path, mode):
    service, client, redis, clock, ops, files, fills = setup(tmp_path, history=mode != "disabled")
    service.history_filler = lambda *a: {"status": "retryable", "reason": "缺数据"}
    assert service.run_record("rec_test").copied == 1
    state = service.store.get_state("rec_test", "source-token")
    suffix = "未填充" if mode == "disabled" else "填充未完成"
    assert service.store.get_step(state, "filled").name.endswith("-" + suffix)
    assert ops[-1][3] == "市场部-备货测试表"


@pytest.mark.parametrize("failure", ["api", "state"])
def test_rename_failure_retries_without_refilling_or_recopying(tmp_path, monkeypatch, failure):
    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    target, attr = (
        (client, "rename_spreadsheet")
        if failure == "api"
        else (service.store, "finish_step_rename")
    )
    actual = getattr(target, attr)
    monkeypatch.setattr(target, attr, lambda *a: (_ for _ in ()).throw(RuntimeError("改名失败")))
    assert service.run_record("rec_test").failed == 1
    assert len(ops) == 2 and fills == ["copy-2"]
    clock.advance(minutes=10)
    service.config = replace(service.config, copy_name_prefix="changed-")
    monkeypatch.setattr(target, attr, actual)
    assert service.run_record("rec_test").copied == 1
    assert len(ops) == 3 and fills == ["copy-2"]
    assert ops[-1][3] == "市场部-备货测试表"
    assert client.renames[-1][1] == "备货测试表-20260821-100000-填充完成"


@pytest.mark.parametrize("force", [False, True])
def test_existing_three_copy_names_remain_unchanged(tmp_path, monkeypatch, force):
    import json

    service, client, redis, clock, ops, files, fills = setup(tmp_path)
    begin = service.store.begin_copy
    monkeypatch.setattr(
        service.store,
        "begin_copy",
        lambda state: begin(
            replace(state, backup_name="", target_name="市场部-备货测试表-oldbatch")
        ),
    )
    copy = client.copy_spreadsheet
    client.copy_spreadsheet = lambda *a, **k: (_ for _ in ()).throw(CopyRejected("首次拒绝"))
    options = {"force": True, "request_id": "old"} if force else {}
    assert service.run_record("rec_test", **options).failed == 1
    key = "test:force:rec_test:old" if force else "test:rec_test:source-token"
    raw = json.loads(redis.get(key))
    raw.pop("backup_name")
    redis.set(key, json.dumps(raw, ensure_ascii=False, sort_keys=True))
    client.copy_spreadsheet = copy
    assert service.run_record("rec_test", **options).copied == 1
    assert [op[3] for op in ops] == [
        "原始备份-市场部-备货测试表-oldbatch",
        "处理备份-市场部-备货测试表-oldbatch",
        "市场部-备货测试表-oldbatch",
    ]
    assert not client.renames


@pytest.mark.parametrize("history", [False, True])
def test_formula_forecast_delivers_filled_copy_once_and_freezes_flags(tmp_path, history):
    service, client, redis, clock, ops, files, calls = setup(
        tmp_path, history=history, forecast=True
    )

    def calculate(state, claim):
        assert claim.history_enabled == history and claim.forecast_enabled
        calls.append(state.target_token)
        files[state.target_token]["forecast"] = 42
        return {"status": "completed"}

    service.forecast_filler = calculate
    result = service.run_record("rec_test")
    assert result.forecast_status == "completed" and not result.fill_degraded
    assert result.history_status == ("completed" if history else "disabled")
    assert files["copy-1"] == {"original": 1}
    assert files["copy-2"] == files["copy-3"] == {"original": 1, "forecast": 42}
    service.config = replace(service.config, fill_forecast_enabled=False)
    repeated = service.run_record("rec_test")
    assert repeated.forecast_status == "completed"
    assert calls == ["copy-2"] and len(ops) == 3


def test_new_history_only_runs_and_reuses_frozen_batch(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path, history=False)
    service.config = replace(service.config, fill_new_history_enabled=True)
    first = service.run_record("rec_test")
    assert first.history_status == "completed" and first.forecast_status == "disabled"
    assert len(fills) == 1 and first.delivery_source == "filled"
    state = service.store.get_state("rec_test", "source-token")
    assert state.new_history_enabled and not state.history_enabled
    service.config = replace(service.config, fill_new_history_enabled=False)
    second = service.run_record("rec_test")
    assert second.history_status == "completed" and len(fills) == 1


def test_new_forecast_skipped_status_survives_delivery_and_repeat(tmp_path):
    service, client, redis, clock, ops, files, fills = setup(tmp_path, forecast=True)
    service.config = replace(service.config, fill_new_history_enabled=True)
    service.forecast_filler = lambda *args: {
        "status": "completed",
        "history_status": "completed",
        "forecast_status": "skipped",
    }
    first = service.run_record("rec_test")
    assert first.forecast_status == "skipped" and not first.fill_degraded
    assert service.run_record("rec_test").forecast_status == "skipped"

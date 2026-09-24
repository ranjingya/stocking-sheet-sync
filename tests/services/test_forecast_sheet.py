from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.domain.models import CopyState, FillState
from stocking_sheet_sync.domain.products import inspect_sheet
from stocking_sheet_sync.domain.sheets.forecast_values import (
    build_forecast_values,
    check_forecast_target,
    project_layout,
)
from stocking_sheet_sync.domain.sheets.layout import (
    build_update,
    dated_forecast_rules,
    plan_market_layout,
)
from stocking_sheet_sync.domain.sheets.validation import verify_sales_update, verify_update
from stocking_sheet_sync.services.calculation import inspect_forecast
from stocking_sheet_sync.services.fill import ForecastFiller
from stocking_sheet_sync.services.notification import summarize_platform_fill
from tests.services.test_forecast_inspect import config as all_config
from tests.services.test_forecast_inspect import fake_reader
from tests.services.test_layout_apply import config, incoming, rules


def setup_sheet():
    before = incoming()
    for row in (4, 6):
        before["cells"][f"A{row}"]["value"] = "KQ25001"
    reader = fake_reader()
    reader.styles.return_value = [
        {key: row[key] for key in ("sku", "style", "name", "spec")} | {"labels": "四季款"}
        for row in inspect_sheet(before, config())["rows"]
    ]
    report = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *all_config())
    layout = build_update(before, config(), rules(), forecast=True)
    prepared = project_layout(before, layout, config(), rules())
    return before, reader, report, layout, prepared


def filled(prepared, update):
    after = deepcopy(prepared)
    for item in update["entries"]:
        after["cells"][item["target_cell"]]["value"] = item["quantity"]
    for item in update["total_entries"]:
        after["cells"][item["target_cell"]].update(
            formula=item["formula"], value=item["expected_quantity"]
        )
    return after


def test_layout_inserts_formula_fields_preserves_manual_and_repeat():
    before, _, _, layout, prepared = setup_sheet()
    assert len(layout["inserted_columns"]) == 20
    assert len(layout["report"]["target_fields"]) == 25
    assert verify_update(before, prepared, layout, config(), rules())["verified"]
    assert build_update(prepared, config(), rules(), forecast=True)["operations"] == []
    assert build_update(prepared, config(), rules())["operations"] == []
    assert not plan_market_layout(prepared, config(), rules(), recent_only=True)["issues"]
    labels = [f["header"] for f in layout["report"]["target_fields"]]
    assert "全公司近30天" not in labels
    assert sum(value.startswith("预估-") for value in labels) == 5
    assert prepared["cells"][f"{layout['column_mapping']['F']}4"]["value"] == 100


@pytest.mark.parametrize("history", [False, True])
def test_independent_history_switch_and_all_cell_preservation(history):
    _, _, report, _, prepared = setup_sheet()
    update = build_forecast_values(
        prepared, report, config(), rules(), history=history, forecast=True
    )
    assert update["summary"]["needs_review"] == 0
    assert len(update["entries"]) == (40 if history else 10)
    assert {e["metric"] for e in update["entries"]} == (
        {"sales", "previous", "future", "forecast"} if history else {"forecast"}
    )
    after = filled(prepared, update)
    assert verify_sales_update(prepared, after, update)["verified"]
    repeated = build_forecast_values(
        after, report, config(), rules(), history=history, forecast=True
    )
    assert repeated["operations"] == []
    changed = deepcopy(after)
    demand = inspect_sheet(after, config())["columns"]["vip"]["demand"] + "4"
    changed["cells"][demand]["value"] = 99
    with pytest.raises(ValueError):
        verify_sales_update(prepared, changed, update)


def test_report_extra_skus_do_not_block_target_rows():
    _, _, report, _, prepared = setup_sheet()
    report["groups"][0]["skus"].append("another-sku")
    checked = check_forecast_target(prepared, report, config())
    assert checked["issues"] == []
    update = build_forecast_values(prepared, report, config(), rules(), history=True, forecast=True)
    assert update["summary"]["needs_review"] == 0
    assert {entry["sku"] for entry in update["entries"]} == {"0004", "0006"}


def test_existing_different_forecast_is_preserved_while_history_can_fill():
    _, _, report, layout, prepared = setup_sheet()
    col = next(
        f["target_column"] for f in layout["report"]["target_fields"] if f["metric"] == "forecast"
    )
    for content in ({"value": 99}, {"formula": "=400", "value": 400}):
        target = deepcopy(prepared)
        target["cells"][col + "4"] = content
        update = build_forecast_values(
            target, report, config(), rules(), history=True, forecast=True
        )
        assert update["status"] == "changes_proposed" and update["operations"]
        assert any(e["target_cell"] == col + "4" for e in update["skipped_forecasts"])
        assert all(e["target_cell"] != col + "4" for e in update["entries"])
        details = summarize_platform_fill(update, config(), history=True, report=report)
        assert details["history"]["status"] == "completed"
        assert details["forecast"]["status"] == "partial"
        assert any(col + "4" in reason for reason in details["forecast"]["reasons"])
        assert target["cells"][col + "4"] == content


def test_missing_recent_window_does_not_use_undated_sheet_history():
    before, reader, report, _, _ = setup_sheet()
    dated = dated_forecast_rules(rules(), report)
    layout = build_update(before, config(), dated, forecast=True)
    prepared = project_layout(before, layout, config(), dated)
    history = build_forecast_values(prepared, report, config(), dated, history=True, forecast=False)
    prepared = filled(prepared, history)
    original = reader.sales_window.side_effect

    def source_with_missing_window(source, skus, start, stop):
        result = original(source, skus, start, stop)
        if source["id"] == "vip" and (stop - start).days == 30:
            result["issues"] = ["snapshot_or_skus_missing"]
        return result

    reader.sales_window.side_effect = source_with_missing_window
    rows = inspect_sheet(prepared, config())["rows"]
    recovered = inspect_forecast(
        reader,
        ["KQ25001"],
        date(2026, 9, 12),
        *all_config(),
        requested_rows=rows,
        snapshot=prepared,
    )
    vip = next(p for p in recovered["groups"][0]["platforms"] if p["platform"] == "vip")
    assert vip["status"] == "needs_review"
    assert "input_origins" not in vip
    assert len([issue for issue in vip["issues"] if "缺少日期证据" in issue]) == 2
    update = build_forecast_values(
        prepared, recovered, config(), dated, history=True, forecast=True, partial=True
    )
    assert "vip" in update["blocked_platforms"]
    assert all(not entry["platform"].startswith("vip:") for entry in update["entries"])
    details = summarize_platform_fill(update, config(), history=True, report=recovered)
    assert "表内历史值缺少日期证据" in details["history"]["reasons"][0]


def test_missing_future_window_uses_only_matching_dated_sheet_history():
    before, reader, report, _, _ = setup_sheet()
    dated = dated_forecast_rules(rules(), report)
    layout = build_update(before, config(), dated, forecast=True)
    prepared = project_layout(before, layout, config(), dated)
    history = build_forecast_values(prepared, report, config(), dated, history=True, forecast=False)
    prepared = filled(prepared, history)
    original = reader.sales_window.side_effect

    def source_with_missing_future(source, skus, start, stop):
        result = original(source, skus, start, stop)
        if source["id"] == "vip" and (stop - start).days != 30:
            result["issues"] = ["incomplete_daily_coverage"]
        return result

    reader.sales_window.side_effect = source_with_missing_future
    rows = inspect_sheet(prepared, config())["rows"]
    recovered = inspect_forecast(
        reader,
        ["KQ25001"],
        date(2026, 9, 12),
        *all_config(),
        requested_rows=rows,
        snapshot=prepared,
    )
    vip = next(p for p in recovered["groups"][0]["platforms"] if p["platform"] == "vip")
    assert vip["status"] == "ready"
    assert vip["input_origins"] == {"historical_future": "sheet"}
    assert vip["forecast"]["total"] == 800
    future_column = next(
        f["target_column"]
        for f in layout["report"]["target_fields"]
        if f["platform"] == "vip" and f["metric"] == "future"
    )
    prepared["cells"][future_column + "3"]["value"] = "唯品25.9.4-26.1.31"
    mismatched_period = inspect_forecast(
        reader,
        ["KQ25001"],
        date(2026, 9, 12),
        *all_config(),
        requested_rows=rows,
        snapshot=prepared,
    )
    vip = next(p for p in mismatched_period["groups"][0]["platforms"] if p["platform"] == "vip")
    assert vip["status"] == "needs_review"
    assert any("表头与预估日不符" in issue for issue in vip["issues"])


def test_matching_history_is_reused_and_only_blank_history_is_filled():
    _, _, report, _, prepared = setup_sheet()
    original = build_forecast_values(
        prepared, report, config(), rules(), history=True, forecast=False
    )
    prepared = filled(prepared, original)
    blank = next(e["target_cell"] for e in original["entries"] if e["platform"] == "vip:previous")
    prepared["cells"][blank] = {}
    update = build_forecast_values(
        prepared, report, config(), rules(), history=True, forecast=True, partial=True
    )
    assert "vip" not in update.get("blocked_platforms", {})
    assert next(e for e in update["entries"] if e["target_cell"] == blank)["status"] == "write"
    assert all(
        e["status"] == "unchanged"
        for e in update["entries"]
        if e["metric"] != "forecast" and e["target_cell"] != blank
    )


def test_existing_history_mismatch_blocks_its_platform_with_cell_detail():
    _, _, report, _, prepared = setup_sheet()
    plan = build_forecast_values(prepared, report, config(), rules(), history=True, forecast=True)
    target = next(e["target_cell"] for e in plan["entries"] if e["platform"] == "vip:sales")
    prepared["cells"][target] = {"value": 99}
    update = build_forecast_values(
        prepared, report, config(), rules(), history=True, forecast=True, partial=True
    )
    assert "vip" in update["blocked_platforms"]
    details = summarize_platform_fill(update, config(), history=True, report=report)
    assert any(target in reason and "表内99" in reason for reason in details["history"]["reasons"])


def test_history_only_does_not_require_available_forecast():
    _, _, report, _, prepared = setup_sheet()
    for p in report["groups"][0]["platforms"]:
        p.pop("forecast")
        p["status"] = "needs_review"
        p["issues"] = ["company_scope_pending_confirmation"]
    update = build_forecast_values(
        prepared, report, config(), rules(), history=True, forecast=False
    )
    assert update["summary"]["needs_review"] == 0
    assert len(update["entries"]) == 30
    blocked = build_forecast_values(
        prepared, report, config(), rules(), history=False, forecast=True
    )
    assert blocked["status"] == "needs_review" and not blocked["operations"]


@pytest.mark.parametrize("manual_platform", [False, True, "missing"])
def test_filler_real_orchestration_uses_one_report_and_no_second_warehouse_read(
    tmp_path, monkeypatch, manual_platform
):
    before, reader, report, layout, prepared = setup_sheet()
    if manual_platform == "missing":
        original_window = reader.daily_window.side_effect

        def missing_window(source, skus, start, stop):
            result = original_window(source, skus, start, stop)
            if start.year == 2026:
                result["issues"] = ["rolling_source_needs_review"]
            return result

        reader.daily_window.side_effect = missing_window
        report = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *all_config())
    elif manual_platform:
        original_window = reader.sales_window.side_effect

        def window(source, skus, start, stop):
            result = original_window(source, skus, start, stop)
            if source["id"] == "vip" and start.year == 2025 and (stop - start).days <= 31:
                for row in result["rows"]:
                    row["quantity"] = 0
            return result

        reader.sales_window.side_effect = window
        report = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *all_config())
    reader.reset_mock()
    dated = dated_forecast_rules(rules(), report)
    layout = build_update(before, config(), dated, forecast=True)
    prepared = project_layout(before, layout, config(), dated)
    update = build_forecast_values(
        prepared, report, config(), rules(), history=True, forecast=True, partial=True
    )
    after = filled(prepared, update)
    filler = ForecastFiller(
        object(),
        config_path=Path("config/config.example.toml"),
        history=True,
        reader_factory=lambda: reader,
    )
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    reads = iter([before, prepared, after])
    monkeypatch.setattr(
        "stocking_sheet_sync.services.fill.read_sheet", lambda *a, **kw: next(reads)
    )
    apply = Mock(return_value={})
    write = Mock(return_value={})
    monkeypatch.setattr("stocking_sheet_sync.services.fill.apply_update", apply)
    monkeypatch.setattr("stocking_sheet_sync.services.fill.write_sales_ranges", write)
    copy = CopyState("r", "s", "name", "url", "record", "copied", target_token="test-token")
    claim = FillState("r", "s", "test-token", "2026-09-12", "attempt", report_path=str(tmp_path))
    result = filler(copy, claim)
    assert result["status"] == "completed"
    assert result["notification_details"]["forecast"]["status"] == (
        "partial" if manual_platform else "completed"
    )
    assert result["notification_details"]["forecast"]["completed"] == (4 if manual_platform else 5)
    assert result["notification_details"]["forecast"]["total"] == 5
    if manual_platform == "missing":
        assert result["notification_details"]["history"]["completed"] == 4
        assert result["notification_details"]["blocked_platforms"] == ["jd_self"]
        assert len(update["entries"]) == 32
        assert all(not e["platform"].startswith("jd_self:") for e in update["entries"])
        for e in update["skipped_entries"]:
            assert after["cells"][e["target_cell"]] == prepared["cells"][e["target_cell"]]
    reader.styles.assert_not_called()
    reader.catalog.assert_called_once()
    assert reader.daily_window.call_count == 3 and reader.sales_window.call_count == 12
    apply.assert_called_once()
    write.assert_called_once()
    assert (Path(claim.report_path) / "values-verification.json").is_file()


def test_filler_blocks_unmatched_requested_sku_before_structural_write(tmp_path, monkeypatch):
    before, reader, _, _, _ = setup_sheet()
    reader.styles.return_value.pop()
    filler = ForecastFiller(
        object(),
        config_path=Path("config/config.example.toml"),
        history=True,
        reader_factory=lambda: reader,
    )
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    monkeypatch.setattr("stocking_sheet_sync.services.fill.read_sheet", lambda *a, **kw: before)
    monkeypatch.setattr(
        "stocking_sheet_sync.services.fill.apply_update", lambda *a, **kw: pytest.fail("不可写入")
    )
    copy = CopyState("r", "s", "name", "url", "record", "copied", target_token="test-token")
    claim = FillState("r", "s", "test-token", "2026-09-12", "attempt", report_path=str(tmp_path))
    assert filler(copy, claim)["status"] == "needs_review"


@pytest.mark.parametrize("existing", [None, 23])
def test_manual_zero_forecast_skips_preserves_and_allows_other_writes(existing):
    _, _, report, layout, prepared = setup_sheet()
    platform = report["groups"][0]["platforms"][0]
    platform.pop("forecast")
    platform.update(status="manual", issues=["previous_sales_zero"])
    col = next(
        f["target_column"]
        for f in layout["report"]["target_fields"]
        if f["platform"] == platform["platform"] and f["metric"] == "forecast"
    )
    if existing is not None:
        prepared["cells"][col + "4"]["value"] = existing
    update = build_forecast_values(prepared, report, config(), rules(), history=True, forecast=True)
    assert update["summary"]["needs_review"] == 0
    assert update["summary"]["manual_forecasts"] == 2
    assert all(e["target_cell"] not in (col + "4", col + "6") for e in update["entries"])
    after = filled(prepared, update)
    assert after["cells"][col + "4"].get("value") == existing
    assert verify_sales_update(prepared, after, update)["verified"]


def test_future_header_dates_migrate_without_inserting_or_changing_values():
    _, _, report, _, prepared = setup_sheet()
    dated = dated_forecast_rules(rules(), report)
    update = build_update(prepared, config(), dated, forecast=True)
    assert not update["inserted_columns"]
    assert len(update["operations"]) == 5
    after = project_layout(prepared, update, config(), dated)
    assert verify_update(prepared, after, update, config(), dated)["verified"]
    assert not build_update(after, config(), dated, forecast=True)["operations"]
    assert not build_update(after, config(), rules())["operations"]
    assert {f["header"] for f in update["report"]["target_fields"] if f["metric"] == "future"} == {
        "唯品25.9.12-26.1.31",
        "自营25.9.12-26.1.31",
        "拼多多25.9.12-26.1.31",
        "猫超25.9.12-26.1.31",
        "京东POP25.9.12-26.1.31",
    }
    assert (
        build_forecast_values(after, report, config(), dated, history=True, forecast=True)[
            "summary"
        ]["needs_review"]
        == 0
    )


def test_future_header_keeps_manual_history_with_same_date_label():
    from stocking_sheet_sync.domain.sheets.layout import _recognize_column

    cells = {
        "A3": {"value": "唯品25.9.12-26.1.31"},
        "B3": {"value": "唯品近30天"},
        "C3": {"value": "唯品会去年同期近30天"},
        "D3": {"value": "唯品25.9.12-26.1.31"},
    }
    assert _recognize_column(cells, 1, 3, config(), rules())[0]["metric"] == "history"
    assert _recognize_column(cells, 4, 3, config(), rules())[0]["metric"] == "future"


def test_future_header_supports_multiple_style_windows_and_exact_end_day():
    _, _, report, _, _ = setup_sheet()
    report = deepcopy(report)
    report["groups"][0]["window"]["history_start"] = "2025-03-04"
    report["groups"][0]["window"]["history_end"] = "2025-09-01"
    other = deepcopy(report["groups"][0])
    other["window"]["history_end"] = "2026-02-01"
    report["groups"].append(other)
    dated = dated_forecast_rules(rules(), report)
    assert dated["forecast"]["future_headers"]["vip"] == "唯品25.3.4-25.8.31、25.3.4-26.1.31"


@pytest.mark.parametrize("failure", ["target", "all_sources", "identity"])
def test_forecast_partial_respects_platform_conflicts_and_global_blockers(failure):
    _, _, report, _, prepared = setup_sheet()
    if failure == "all_sources":
        for platform in report["groups"][0]["platforms"]:
            platform.update(status="needs_review", issues=["rolling_source_needs_review"])
    elif failure == "identity":
        report["groups"][0]["catalog"] = []
    else:
        full = build_forecast_values(
            prepared, report, config(), rules(), history=True, forecast=True
        )
        cell = full["entries"][0]["target_cell"]
        prepared["cells"][cell] = {"value": 99999}
    update = build_forecast_values(
        prepared, report, config(), rules(), history=True, forecast=True, partial=True
    )
    if failure == "target":
        assert update["status"] == "partial"
        assert len(update["entries"]) == 32
        assert verify_sales_update(prepared, filled(prepared, update), update)["verified"]
    else:
        assert update["summary"]["needs_review"]
        assert not update["operations"]


def test_selected_forecast_overwrites_only_requested_platform_prediction():
    _, _, report, _, prepared = setup_sheet()
    cfg = {**config(), "selected_platforms": {"vip"}, "overwrite": True}
    for group in report["groups"]:
        group["platforms"] = [p for p in group["platforms"] if p["platform"] == "vip"]
    plan = build_forecast_values(prepared, report, cfg, rules(), history=False, forecast=True)
    assert len(plan["entries"]) == 2
    for e in plan["entries"]:
        prepared["cells"][e["target_cell"]] = {"value": 999}
    plan = build_forecast_values(prepared, report, cfg, rules(), history=False, forecast=True)
    assert plan["summary"]["needs_review"] == 0
    assert {e["platform"] for e in plan["entries"]} == {"vip:forecast"}
    assert verify_sales_update(prepared, filled(prepared, plan), plan)["verified"]


def test_platform_selection_only_adds_selected_platform_columns():
    before, _, _, _, _ = setup_sheet()
    cfg = {**config(), "selected_platforms": {"vip"}}
    update = build_update(before, cfg, rules(), forecast=True)
    added = [f for f in update["report"]["target_fields"] if not f["source_column"]]
    assert {f["platform"] for f in added} == {"vip"}
    assert len(added) == 4
    prepared = project_layout(before, update, cfg, rules())
    assert verify_update(before, prepared, update, cfg, rules())["verified"]

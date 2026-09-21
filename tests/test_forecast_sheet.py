from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.forecast_fill import ForecastFiller
from stocking_sheet_sync.forecast_inspect import inspect_forecast
from stocking_sheet_sync.forecast_sheet import (
    build_forecast_values,
    check_forecast_target,
    project_layout,
)
from stocking_sheet_sync.layout_apply import build_update, verify_update
from stocking_sheet_sync.models import CopyState, FillState
from stocking_sheet_sync.sales_fill import verify_sales_update
from stocking_sheet_sync.sheet_layout import dated_forecast_rules, plan_market_layout
from stocking_sheet_sync.sheet_matching import inspect_sheet
from tests.test_forecast_inspect import config as all_config
from tests.test_forecast_inspect import fake_reader
from tests.test_layout_apply import config, incoming, rules


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


def test_existing_quantity_or_formula_blocks_without_replacing():
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
        assert update["status"] == "needs_review" and not update["operations"]
        assert target["cells"][col + "4"] == content


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


def test_filler_real_orchestration_uses_one_report_and_no_second_warehouse_read(
    tmp_path, monkeypatch
):
    before, reader, report, layout, prepared = setup_sheet()
    reader.reset_mock()
    dated = dated_forecast_rules(rules(), report)
    layout = build_update(before, config(), dated, forecast=True)
    prepared = project_layout(before, layout, config(), dated)
    update = build_forecast_values(prepared, report, config(), rules(), history=True, forecast=True)
    after = filled(prepared, update)
    filler = ForecastFiller(object(), history=True, reader_factory=lambda: reader)
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    reads = iter([before, prepared, after])
    monkeypatch.setattr(
        "stocking_sheet_sync.forecast_fill.read_sheet", lambda *a, **kw: next(reads)
    )
    apply = Mock(return_value={})
    write = Mock(return_value={})
    monkeypatch.setattr("stocking_sheet_sync.forecast_fill.apply_update", apply)
    monkeypatch.setattr("stocking_sheet_sync.forecast_fill.write_sales_ranges", write)
    copy = CopyState("r", "s", "name", "url", "record", "copied", target_token="test-token")
    claim = FillState("r", "s", "test-token", "2026-09-12", "attempt", report_path=str(tmp_path))
    assert filler(copy, claim)["status"] == "completed"
    reader.styles.assert_not_called()
    reader.catalog.assert_called_once()
    assert reader.daily_window.call_count == 3 and reader.sales_window.call_count == 12
    apply.assert_called_once()
    write.assert_called_once()
    assert (Path(claim.report_path) / "values-verification.json").is_file()


def test_filler_blocks_unmatched_requested_sku_before_structural_write(tmp_path, monkeypatch):
    before, reader, _, _, _ = setup_sheet()
    reader.styles.return_value.pop()
    filler = ForecastFiller(object(), history=True, reader_factory=lambda: reader)
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    monkeypatch.setattr("stocking_sheet_sync.forecast_fill.read_sheet", lambda *a, **kw: before)
    monkeypatch.setattr(
        "stocking_sheet_sync.forecast_fill.apply_update", lambda *a, **kw: pytest.fail("不可写入")
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
    from stocking_sheet_sync.sheet_layout import _recognize_column

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

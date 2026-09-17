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
from stocking_sheet_sync.sheet_layout import plan_market_layout
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
    assert len(layout["inserted_columns"]) == 21
    assert len(layout["report"]["target_fields"]) == 26
    assert verify_update(before, prepared, layout, config(), rules())["verified"]
    assert build_update(prepared, config(), rules(), forecast=True)["operations"] == []
    assert build_update(prepared, config(), rules())["operations"] == []
    assert not plan_market_layout(prepared, config(), rules(), recent_only=True)["issues"]
    labels = [f["header"] for f in layout["report"]["target_fields"]]
    assert labels[0] == "全公司近30天"
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
    assert after["cells"]["F4"].get("value") is None
    changed = deepcopy(after)
    demand = inspect_sheet(after, config())["columns"]["vip"]["demand"] + "4"
    changed["cells"][demand]["value"] = 99
    with pytest.raises(ValueError):
        verify_sales_update(prepared, changed, update)


def test_missing_or_extra_sku_blocks_all_write():
    _, _, report, _, prepared = setup_sheet()
    report["groups"][0]["skus"].append("another-sku")
    checked = check_forecast_target(prepared, report, config())
    assert checked["issues"][0]["missing_skus"] == ["another-sku"]
    update = build_forecast_values(prepared, report, config(), rules(), history=True, forecast=True)
    assert update["status"] == "needs_review" and not update["operations"]


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
    assert reader.styles.call_count == 1
    assert reader.daily_window.call_count == 3 and reader.sales_window.call_count == 12
    apply.assert_called_once()
    write.assert_called_once()
    assert (Path(claim.report_path) / "values-verification.json").is_file()


def test_filler_blocks_sku_gap_before_structural_write(tmp_path, monkeypatch):
    before, reader, _, _, _ = setup_sheet()
    reader.styles.return_value.append({**reader.styles.return_value[0], "sku": "extra"})
    filler = ForecastFiller(object(), history=True, reader_factory=lambda: reader)
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    monkeypatch.setattr("stocking_sheet_sync.forecast_fill.read_sheet", lambda *a, **kw: before)
    monkeypatch.setattr(
        "stocking_sheet_sync.forecast_fill.apply_update", lambda *a, **kw: pytest.fail("不可写入")
    )
    copy = CopyState("r", "s", "name", "url", "record", "copied", target_token="test-token")
    claim = FillState("r", "s", "test-token", "2026-09-12", "attempt", report_path=str(tmp_path))
    assert filler(copy, claim)["status"] == "needs_review"

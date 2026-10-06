from copy import deepcopy
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.domain.products import column_name
from stocking_sheet_sync.domain.sheets.totals import supplement_row_summaries
from stocking_sheet_sync.domain.sheets.validation import verify_sales_update
from stocking_sheet_sync.infrastructure.feishu.sheets import write_sales_ranges


def sample():
    snapshot = {
        "spreadsheet_token": "sample",
        "layout": {},
        "revision": 1,
        "sheet_id": "test",
        "column_count": 7,
        "row_count": 5,
        "merges": ["B2:D2", "G1:G3"],
        "cells": {f"{c}{r}": {} for c in "ABCDEFG" for r in range(1, 6)},
    }
    snapshot["cells"].update(
        {
            "A3": {"value": "天猫"},
            "B2": {"value": "拼多多"},
            "B3": {"value": "拼多多近30天"},
            "C3": {"value": "预估-拼多多"},
            "D3": {"value": "拼多多"},
            "E3": {"value": "批发"},
            "F3": {"value": "全公司近30天"},
            "G1": {"value": "需求汇总"},
            "A4": {"value": 10},
            "B4": {"value": 100},
            "C4": {"value": 200},
            "D4": {"formula": "=10+10", "value": 20},
            "E4": {"value": 30},
            "F4": {"value": 300},
            "G4": {"value": 660, "formula": "=SUM(A4:F4)", "cell_styles": {"bold": True}},
        }
    )
    config = {
        "matching": {
            "header_rows": 3,
            "summary_headers": ["需求汇总"],
            "other_demand_headers": ["天猫", "批发"],
        },
        "platforms": [{"demand_headers": ["拼多多"]}],
    }
    update = {
        "as_of": "2026-10-06",
        "summary": {"needs_review": 0, "platform_totals": {}},
        "total_entries": [],
        "entries": [],
        "operations": [],
        "status": "unchanged",
    }
    return snapshot, config, update


def test_summary_only_manual_columns_preserves_values_and_styles_and_is_idempotent():
    before, config, update = sample()
    original = deepcopy(before)
    supplement_row_summaries(update, before, config, [4])
    assert before == original
    assert update["summary_replacements"] == {"G4": "=SUM(A4:F4)"}
    total = update["total_entries"][0]
    assert total["formula"] == "=SUM(A4,D4,E4)"
    assert total["expected_quantity"] == 60
    after = deepcopy(before)
    after["cells"]["G4"].update(formula=total["formula"], value=60)
    assert verify_sales_update(before, after, update)["verified"]
    _, _, again = sample()
    supplement_row_summaries(again, after, config, [4])
    assert not again["operations"]


def test_summary_ignores_text_blanks_and_preserves_zero_like_sum():
    before, config, update = sample()
    before["cells"]["A4"] = {"value": "10"}
    before["cells"]["D4"] = {"value": 0}
    before["cells"]["E4"] = {}
    supplement_row_summaries(update, before, config, [4])
    assert update["total_entries"][0]["expected_quantity"] == 0


@pytest.mark.parametrize("case", ["ambiguous", "missing_demand", "error"])
def test_summary_rejects_ambiguous_or_invalid_inputs(case):
    before, config, update = sample()
    if case == "ambiguous":
        before["cells"]["F3"] = {"value": "需求汇总"}
    elif case == "missing_demand":
        config["matching"]["other_demand_headers"] = []
        config["platforms"] = []
    else:
        before["cells"]["D4"] = {"value": "#VALUE!"}
    with pytest.raises(ValueError):
        supplement_row_summaries(update, before, config, [4])


def test_summary_missing_header_or_blocked_plan_does_not_write():
    before, config, update = sample()
    config["matching"]["summary_headers"] = []
    supplement_row_summaries(update, before, config, [4])
    assert not update["operations"]
    config["matching"]["summary_headers"] = ["需求汇总"]
    update["summary"]["needs_review"] = 1
    supplement_row_summaries(update, before, config, [4])
    assert not update["operations"]


@pytest.mark.parametrize("actual", ["=SUM(A4:F4)", "=SUM(A4:E4)", None])
def test_summary_write_requires_exact_previewed_old_formula(actual):
    before, config, update = sample()
    supplement_row_summaries(update, before, config, [4])
    client = Mock()
    client._request.return_value = {
        "revision": 1,
        "valueRanges": [{"range": "test!G4:G4", "values": [[actual]]}],
    }
    args = dict(
        expected_revision=1, client=client, summary_replacements=update["summary_replacements"]
    )
    if actual == "=SUM(A4:F4)":
        write_sales_ranges("token", "test", update["operations"], **args)
        assert client._request.call_count == 2
    else:
        with pytest.raises(ValueError, match="原值发生变化"):
            write_sales_ranges("token", "test", update["operations"], **args)
        assert client._request.call_count == 1


@pytest.mark.parametrize(
    "formula", ["=SUM(A3,D4)", "=SUM(A4,G4)", "=SUM(A4:A5)", "=SUM(A4,A4)", "=SUM(H4)"]
)
def test_summary_write_rejects_non_row_or_self_references(formula):
    client = Mock()
    with pytest.raises(ValueError, match="同行左侧"):
        write_sales_ranges(
            "token",
            "test",
            [{"range": "test!G4:G4", "values": [[{"type": "formula", "text": formula}]]}],
            expected_revision=1,
            client=client,
            summary_replacements={"G4": None},
        )
    client._request.assert_not_called()


def test_row_formula_without_explicit_authorization_is_rejected():
    before, config, update = sample()
    supplement_row_summaries(update, before, config, [4])
    client = Mock()
    with pytest.raises(ValueError):
        write_sales_ranges(
            "token", "test", update["operations"], expected_revision=1, client=client
        )
    client._request.assert_not_called()


@pytest.mark.parametrize("forecast", [False, True])
def test_both_fill_pipelines_include_row_summary(forecast):
    from stocking_sheet_sync.domain.sheets.forecast_values import build_forecast_values
    from stocking_sheet_sync.domain.sheets.values import build_sales_update
    from tests.services.test_forecast_sheet import setup_sheet
    from tests.services.test_layout_apply import config as forecast_config
    from tests.services.test_layout_apply import rules
    from tests.services.test_sales_fill import config as sales_config
    from tests.services.test_sales_fill import ready

    if forecast:
        _, _, report, _, before = setup_sheet()
        config = forecast_config()
    else:
        before, report = ready()
        config = sales_config()
    config["matching"]["summary_headers"] = ["需求汇总"]
    before["column_count"] += 1
    col = column_name(before["column_count"])
    for row in range(1, before["row_count"] + 1):
        before["cells"][f"{col}{row}"] = {}
    before["cells"][f"{col}3"] = {"value": "需求汇总"}
    update = (
        build_forecast_values(before, report, config, rules(), history=True, forecast=True)
        if forecast
        else build_sales_update(before, report, config)
    )
    assert set(update["summary_replacements"]) == {f"{col}4", f"{col}6"}
    summaries = [e for e in update["total_entries"] if e["platform"] == "demand_summary"]
    assert len(summaries) == 2
    assert all(len(e["source_cells"]) == 5 for e in summaries)


def test_summary_verifies_recalculated_manual_formula():
    before, config, update = sample()
    supplement_row_summaries(update, before, config, [4])
    after = deepcopy(before)
    after["cells"]["D4"]["value"] = 50
    after["cells"]["G4"].update(formula="=SUM(A4,D4,E4)", value=90)
    assert verify_sales_update(before, after, update)["verified"]
    after["cells"]["G4"]["value"] = 60
    with pytest.raises(ValueError, match="计算值"):
        verify_sales_update(before, after, update)


def test_summary_missing_preflight_original_is_rejected():
    before, config, update = sample()
    supplement_row_summaries(update, before, config, [4])
    client = Mock()
    client._request.return_value = {
        "revision": 1,
        "valueRanges": [
            {"range": "test!G4:G4", "values": []},
        ],
    }
    with pytest.raises(ValueError, match="回读缺失"):
        write_sales_ranges(
            "token",
            "test",
            update["operations"],
            expected_revision=1,
            client=client,
            summary_replacements=update["summary_replacements"],
        )

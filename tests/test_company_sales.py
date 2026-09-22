from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from stocking_sheet_sync.domain.sheets.company import read_company_sales
from stocking_sheet_sync.services.calculation import inspect_forecast
from stocking_sheet_sync.settings import load_forecast_config
from tests.test_forecast_inspect import config, fake_reader


def company_sheet(values=(10, 30)):
    return {
        "column_count": 3,
        "cells": {
            "C1": {"value": "26年销售总数"},
            "C3": {"value": "25.9.1-26.1.31"},
            "C4": {"value": values[0]},
            "C5": {"value": values[1]},
        },
    }


def requested():
    return [
        {"row": i + 4, "issues": [], **row}
        for i, row in enumerate(fake_reader().styles.return_value)
    ]


def test_company_lifecycle_values_read_by_sheet_row_preserve_source():
    snapshot = company_sheet()
    before = deepcopy(snapshot)
    result = read_company_sales(
        snapshot, requested(), load_forecast_config(Path("config/config.example.toml"))
    )
    assert result["status"] == "available"
    assert result["period"] == "25.9.1-26.1.31"
    assert [r["quantity"] for r in result["rows"]] == [10, 30]
    assert snapshot == before


@pytest.mark.parametrize("values", [(None, 2), ("", 2), (True, 2), (-1, 2)])
def test_company_invalid_cell_not_assumed_zero(values):
    result = read_company_sales(
        company_sheet(values), requested(), load_forecast_config(Path("config/config.example.toml"))
    )
    assert result["rows"][0]["quantity"] is None
    assert result["rows"][0]["issue"]


def test_multiple_company_columns_are_ambiguous():
    snapshot = company_sheet()
    snapshot["cells"]["B1"] = {"value": "25年销售总数"}
    assert (
        read_company_sales(
            snapshot, requested(), load_forecast_config(Path("config/config.example.toml"))
        )["status"]
        == "ambiguous"
    )


@pytest.mark.parametrize("values,manual", [((10, 30), False), ((None, 30), True), ((0, 0), True)])
def test_company_fallback_uses_sheet_lifecycle_or_leaves_manual(values, manual):
    reader = fake_reader()
    original = reader.sales_window.side_effect

    def window(source, skus, start, stop):
        result = original(source, skus, start, stop)
        if source["id"] == "pdd" and start.year == 2026:
            for row in result["rows"]:
                row["quantity"] = 2
        return result

    reader.sales_window.side_effect = window
    result = inspect_forecast(
        reader,
        [],
        date(2026, 9, 12),
        *config(),
        requested_rows=requested(),
        snapshot=company_sheet(values),
    )
    pdd = next(p for p in result["groups"][0]["platforms"] if p["platform"] == "pdd")
    if manual:
        assert pdd["status"] == "manual" and "forecast" not in pdd
    else:
        assert pdd["status"] == "ready"
        assert pdd["forecast"]["share_source"] == "company"
        assert pdd["forecast"]["total"] == 40
        assert [pdd["forecast"]["rows"][sku]["quantity"] for sku in ("001", "002")] == [10, 30]

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from stocking_sheet_sync.domain.products import (
    cells_from_envelope,
    column_name,
    inspect_sheet,
    match_catalog,
    sku_text,
)
from stocking_sheet_sync.services.sales import inspect_sales
from stocking_sheet_sync.settings import load_sales_config

CONFIG = Path(__file__).resolve().parents[1] / "config/config.example.toml"


def config():
    return load_sales_config(CONFIG)


def snapshot():
    cfg = config()
    cells = {f"{column_name(c)}{r}": {} for c in range(1, 15) for r in range(1, 8)}
    for col, title in zip("ABCD", ["款式编码", "商品编码", "商品名称", "颜色规格"], strict=True):
        cells[f"{col}3"] = {"value": title}
    cells["E2"] = {"value": "市场部"}
    for i, platform in enumerate(cfg["platforms"]):
        cells[f"{column_name(5 + i * 2)}3"] = {"value": platform["sales_headers"][0]}
        cells[f"{column_name(6 + i * 2)}3"] = {"value": platform["demand_headers"][0]}
    for row, code in [(4, "00123"), (6, "A-456")]:
        for col, value in zip("ABCD", ["STYLE", code, "测试商品", "蓝色 M"], strict=True):
            cells[f"{col}{row}"] = {"value": value}
    cells["C7"] = {"value": "合计"}
    return {
        "sheet_id": "test",
        "row_count": 7,
        "column_count": 14,
        "cells": cells,
        "merges": ["E2:N2"],
    }


def catalog():
    return [
        {"sku": code, "style": "STYLE", "name": "测试商品", "spec": "蓝色M"}
        for code in ("00123", "A-456")
    ]


def test_mapping_includes_rows_after_blank_and_keeps_leading_zeros():
    layout = inspect_sheet(snapshot(), config())
    match_catalog(layout, catalog())
    assert [r["row"] for r in layout["rows"]] == [4, 6]
    assert [r["sku"] for r in layout["rows"]] == ["00123", "A-456"]
    assert all(r["match_status"] == "matched" for r in layout["rows"])
    assert layout["columns"]["jd_self"] == {"sales": "E", "demand": "F"}
    assert layout["issues"] == []


def test_combined_other_column_never_substitutes_for_pop_or_supermarket():
    sheet = snapshot()
    for cell in ("G3", "H3", "M3", "N3"):
        sheet["cells"][cell] = {}
    sheet["cells"]["G3"] = {"value": "市场部其它"}
    layout = inspect_sheet(sheet, config())
    assert layout["columns"]["jd_pop"] == {"sales": None, "demand": None}
    assert layout["columns"]["tmall_supermarket"] == {"sales": None, "demand": None}
    assert layout["combined_columns"] == ["G"]
    assert len(layout["issues"]) == 4


def test_duplicate_sheet_sku_and_catalog_conflict_are_reported():
    sheet = snapshot()
    sheet["cells"]["B6"]["value"] = "00123"
    layout = inspect_sheet(sheet, config())
    data = catalog()
    data.append({**data[0], "style": "OTHER"})
    match_catalog(layout, data)
    assert all("duplicate_sheet_sku" in r["issues"] for r in layout["rows"])
    assert all("catalog_sku_ambiguous" in r["issues"] for r in layout["rows"])


def test_same_name_cannot_substitute_for_different_code():
    layout = inspect_sheet(snapshot(), config())
    match_catalog(layout, [{**catalog()[0], "sku": "123"}])
    assert all("catalog_sku_missing" in r["issues"] for r in layout["rows"])


@pytest.mark.parametrize("value", [1.5, 10**15, "6.9426E+12", True, None])
def test_unsafe_sku_representation_is_rejected(value):
    with pytest.raises(ValueError):
        sku_text(value)


def test_truncated_or_skipped_coordinates_are_rejected():
    payload = {
        "ok": True,
        "data": {
            "has_more": False,
            "ranges": [
                {
                    "actual_range": "B4:C4",
                    "row_indices": [4],
                    "col_indices": ["B", "C"],
                    "cells": [[{"value": "00123"}, {}]],
                    "truncated": False,
                }
            ],
        },
    }
    assert cells_from_envelope(payload)["B4"]["value"] == "00123"
    truncated = deepcopy(payload)
    truncated["data"]["has_more"] = True
    with pytest.raises(ValueError, match="截断"):
        cells_from_envelope(truncated)
    payload["data"]["ranges"][0]["row_indices"] = [5]
    with pytest.raises(ValueError, match="缺口"):
        cells_from_envelope(payload)


class Reader:
    def catalog(self, source, skus):
        return catalog()

    def sales(self, source, skus, as_of):
        return {"platform": source["id"], "rows": [], "issues": []}


def test_no_shipments_is_zero_only_with_catalog_and_daily_coverage():
    report = inspect_sales(Reader(), snapshot(), config(), date(2026, 9, 5))
    details = [e for e in report["entries"] if e["platform"] != "jd_self"]
    assert all(e["candidate_quantity"] == 0 for e in details)
    assert all(
        e["status"] == "needs_review" for e in report["entries"] if e["platform"] == "jd_self"
    )


def test_incomplete_source_never_becomes_zero():
    class Missing(Reader):
        def sales(self, *args):
            return {"rows": [], "issues": ["incomplete_daily_coverage"]}

    report = inspect_sales(Missing(), snapshot(), config(), date(2026, 9, 5))
    assert all(e["candidate_quantity"] is None for e in report["entries"])
    assert all(e["observed_quantity"] is None for e in report["entries"])


def test_existing_zero_and_formula_are_protected():
    sheet = snapshot()
    sheet["cells"]["G4"] = {"value": 0}
    sheet["cells"]["G6"] = {"formula": "=0"}
    report = inspect_sales(Reader(), sheet, config(), date(2026, 9, 5))
    rows = [e for e in report["entries"] if e["platform"] == "jd_pop"]
    assert all("target_not_empty" in e["issues"] for e in rows)
    assert all(e["candidate_quantity"] is None for e in rows)


def test_partial_local_snapshot_is_rejected():
    sheet = snapshot()
    del sheet["cells"]["B6"]
    with pytest.raises(ValueError, match="完整工作表"):
        inspect_sheet(sheet, config())


def test_ambiguous_sales_columns_are_not_assigned():
    sheet = snapshot()
    sheet["cells"]["G3"] = sheet["cells"]["E3"]
    layout = inspect_sheet(sheet, config())
    assert layout["columns"]["jd_self"]["sales"] is None
    assert any(i["reason"] == "ambiguous_column" for i in layout["issues"])


def test_matching_rejects_style_or_spec_mismatch():
    layout = inspect_sheet(snapshot(), config())
    data = catalog()
    data[0]["spec"] = "红色L"
    match_catalog(layout, data)
    assert "spec_mismatch" in layout["rows"][0]["issues"]
    assert layout["rows"][0]["match_status"] == "needs_review"

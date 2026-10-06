"""全公司ADS与京东近30天汇总的来源校验。"""

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.infrastructure.company import CompanyReader
from stocking_sheet_sync.settings import business_view, load_forecast_sources


def provider():
    path = Path("config/config.example.toml")
    reader = Mock()
    reader._read.return_value = [{"sku": "A", "quantity": 12}, {"sku": "B", "quantity": 0}]
    reader.daily_window.return_value = {
        "rows": [
            {"sku": "A", "quantity": 3, "status": "matched"},
            {"sku": "B", "quantity": 0, "status": "matched"},
        ]
    }
    return CompanyReader(
        reader, business_view(path, "company"), load_forecast_sources(path)["daily"]["jd_self"]
    )


def test_company_combines_same_day_rolling_totals_and_keeps_zero():
    p = provider()
    rows = p.window(["A", "B"], date(2026, 9, 6), date(2026, 10, 6))
    assert [row["quantity"] for row in rows] == [15, 0]
    sql, params = p.reader._read.call_args.args
    assert "ads_whs_outstock_gs_base_sku_window" in sql
    assert "`sales_out_qty_30d`" in sql and "`stat_date`" in sql and "`spec_code`" in sql
    assert params == ("2026-10-05", "2026-10-06", "A", "B")
    assert p.reader._read.call_count == 1
    source, skus, start, stop = p.reader.daily_window.call_args.args
    assert source["table"] == "jd_inventory_product_detail"
    assert source["connection"] == "mysql"
    assert source["rolling_fields"]["30"] == "outbound_30d"
    assert source["business_date_offset_days"] == 0
    assert skus == ["A", "B"] and (stop - start).days == 30
    assert rows[0]["company"]["snapshot_date"] == "2026-10-05"


@pytest.mark.parametrize("value", [None, -1, 1.5, "bad", float("nan")])
def test_invalid_ads_quantity_does_not_block_other_sku(value):
    p = provider()
    p.reader._read.return_value[0]["quantity"] = value
    rows = p.window(["A", "B"], date(2026, 9, 6), date(2026, 10, 6))
    assert rows[0]["quantity"] is None
    assert "公司ADS近30天数量无效" in rows[0]["issues"]
    assert rows[1]["quantity"] == 0


@pytest.mark.parametrize("kind", ["empty", "missing", "duplicate", "jd_missing", "jd_duplicate"])
def test_missing_and_duplicate_snapshots_are_not_zero(kind):
    p = provider()
    if kind == "empty":
        p.reader._read.return_value = []
    elif kind == "missing":
        p.reader._read.return_value = [{"sku": "B", "quantity": 0}]
    elif kind == "duplicate":
        p.reader._read.return_value.append({"sku": "A", "quantity": 12})
    elif kind == "jd_missing":
        p.reader.daily_window.return_value["rows"] = [
            {"sku": "B", "quantity": 0, "status": "matched"}
        ]
    else:
        p.reader.daily_window.return_value["rows"].append(
            {"sku": "A", "quantity": 3, "status": "matched"}
        )
    rows = p.window(["A", "B"], date(2026, 9, 6), date(2026, 10, 6))
    assert rows[0]["quantity"] is None
    assert rows[1]["quantity"] == (None if kind == "empty" else 0)
    assert p.reader._read.call_count == 1


def test_company_rejects_special_period_without_reading_source():
    p = provider()
    with pytest.raises(ValueError, match="30天"):
        p.window(["A"], date(2025, 9, 28), date(2026, 2, 1))
    p.reader._read.assert_not_called()
    p.reader.daily_window.assert_not_called()

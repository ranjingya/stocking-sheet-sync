import csv
import json
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.infrastructure.warehouse import ForecastReader
from stocking_sheet_sync.services.calculation import (
    inspect_forecast,
    quantities,
    write_forecast_report,
)
from stocking_sheet_sync.settings import (
    WarehouseSettings,
    load_forecast_config,
    load_forecast_sources,
    load_layout_config,
    load_sales_config,
)


def config():
    sales = load_sales_config(Path("config/config.example.toml"))
    return (
        sales,
        load_forecast_sources(Path("config/config.example.toml")),
        load_forecast_config(Path("config/config.example.toml")),
        load_layout_config(Path("config/config.example.toml"), sales),
    )


def fake_reader():
    reader = Mock()
    reader.styles.return_value = [
        {"sku": sku, "style": "KQ25001", "name": "商品", "spec": sku, "labels": "四季款"}
        for sku in ("001", "002")
    ]

    def window(source, skus, start, stop):
        value = 40 if start.year == 2026 else 10 if (stop - start).days == 30 else 100
        return {
            "issues": [],
            "rows": [{"sku": sku, "quantity": value, "status": "matched"} for sku in skus],
        }

    reader.catalog.side_effect = lambda source, skus: [
        row for row in reader.styles.return_value if row["sku"] in skus
    ]
    reader.sales_window.side_effect = window
    reader.daily_window.side_effect = window
    return reader


def test_read_only_trial_partitions_platforms_and_preserves_evidence(tmp_path):
    reader = fake_reader()
    result = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *config())
    assert result["summary"]["ready"] == 5
    group = result["groups"][0]
    assert group["window"]["history_start"] == "2025-09-12"
    assert group["window"]["history_end"] == "2026-02-01"
    for platform in group["platforms"]:
        assert platform["forecast"]["total"] == 800
        assert platform["forecast"]["rows"]["001"]["quantity"] == 400
        assert len(platform["sources"]) == 3
    assert reader.daily_window.call_count == 3
    assert reader.sales_window.call_count == 12
    jd_calls = [call for call in reader.daily_window.call_args_list]
    assert all(
        call.args[0]["table"] == "jd_inventory_product_detail"
        and call.args[0]["connection"] == "mysql"
        and "summary" not in call.args[0]
        for call in jd_calls
    )
    assert {call.args[2].year for call in jd_calls} == {2025, 2026}
    write_forecast_report(tmp_path, result)
    assert json.loads((tmp_path / "forecast.json").read_text())["summary"]["ready"] == 5
    with (tmp_path / "forecast.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 10 and rows[0]["sku"] == "001"
    assert {row["forecast"] for row in rows} == {"400"}


def test_company_pending_blocks_only_fallback_platform():
    reader = fake_reader()
    original = reader.sales_window.side_effect

    def sales(source, skus, start, stop):
        result = original(source, skus, start, stop)
        if source["id"] == "pdd" and start.year == 2026:
            for row in result["rows"]:
                row["quantity"] = 2
        return result

    reader.sales_window.side_effect = sales
    result = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *config())
    assert result["summary"]["ready"] == 4
    pdd = next(p for p in result["groups"][0]["platforms"] if p["platform"] == "pdd")
    assert pdd["issues"] == ["company_lifecycle_sales_unavailable"]
    assert pdd["status"] == "manual"
    assert "forecast" not in pdd
    assert pdd["inputs"]["current"] == {"001": 2, "002": 2}


def test_catalog_conflict_and_unknown_styles_never_read_sales():
    reader = fake_reader()
    reader.styles.return_value[0]["labels"] = "秋冬款"
    result = inspect_forecast(reader, ["KQ25001", "KQ26001"], date(2026, 9, 12), *config())
    assert result["summary"]["platform_results"] == 0
    assert all(g["issues"] for g in result["groups"])
    reader.daily_window.assert_not_called()
    reader.sales_window.assert_not_called()


def test_duplicate_catalog_identity_not_arbitrarily_selected():
    reader = fake_reader()
    row = {**reader.styles.return_value[0], "spec": "另一规格"}
    reader.styles.return_value.append(row)
    result = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *config())
    assert result["groups"][0]["issues"] == ["catalog_identity_or_labels_conflict"]
    reader.sales_window.assert_not_called()


def test_source_failure_isolated_from_other_platforms():
    reader = fake_reader()
    reader.daily_window.side_effect = RuntimeError("连接失败")
    result = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *config())
    assert result["summary"]["ready"] == 4
    jd = next(p for p in result["groups"][0]["platforms"] if p["platform"] == "jd_self")
    assert len(jd["issues"]) == 3 and "forecast" not in jd


def test_missing_detail_zero_only_with_complete_source():
    assert quantities({"rows": [], "issues": []}, ["001"], absent_is_zero=True) == {"001": 0}
    with pytest.raises(ValueError):
        quantities({"rows": [], "issues": ["missing_dates"]}, ["001"], absent_is_zero=True)
    with pytest.raises(ValueError):
        quantities({"rows": [], "issues": []}, ["001"], absent_is_zero=False)


def daily_reader():
    reader = ForecastReader(WarehouseSettings("unused", 3306, "unused", "unused", "unused"))
    source = config()[1]["daily"]["jd_self"]
    source.pop("rolling_fields")
    start = date(2026, 8, 13)
    rows = [
        {
            "date": start + timedelta(days=n),
            "sku": "001",
            "row_id": "jd-1",
            "quantity": 2,
            "rolling_30": 60,
        }
        for n in range(30)
    ]
    reader._read = Mock(return_value=rows)
    reader._read_mysql = reader._read
    return reader, source, start, rows


def test_daily_dates_are_business_dates_and_sum_reconciled():
    reader, source, start, _ = daily_reader()
    result = reader.daily_window(source, ["001"], start, start + timedelta(days=30))
    assert result["issues"] == []
    assert result["rows"][0]["quantity"] == 60
    sql, params = reader._read.call_args.args
    assert "outbound_yesterday" in sql and "001" not in sql
    assert params[-3:] == ("2026-08-13", "2026-09-12", "001")


@pytest.mark.parametrize("problem", ["missing", "duplicate", "invalid", "null_id"])
def test_daily_integrity_blocks_quantity(problem):
    reader, source, start, rows = daily_reader()
    expected = {
        "missing": "incomplete_sku_daily_coverage",
        "duplicate": "duplicate_source_sku_day",
        "invalid": "invalid_daily_value",
        "null_id": "invalid_daily_value",
    }
    if problem == "missing":
        rows.pop(0)
    elif problem == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif problem == "invalid":
        rows[0]["quantity"] = -1
    else:
        rows[0]["row_id"] = None
    result = reader.daily_window(source, ["001"], start, start + timedelta(days=30))
    assert result["issues"]
    assert expected[problem] in result["rows"][0]["issues"]
    assert result["rows"][0]["quantity"] is None


def test_daily_missing_sku_is_not_zero_and_large_window_does_not_sum_rolling():
    reader, source, start, rows = daily_reader()
    rows[-1]["rolling_30"] = 999
    result = reader.daily_window(source, ["001", "002"], start, start + timedelta(days=29))
    assert result["rows"][0]["quantity"] == 58
    assert result["rows"][1]["quantity"] is None
    assert len(result["rows"][1]["missing_dates"]) == 29


def test_catalog_query_binds_style_and_selects_all_skus():
    reader, _, _, _ = daily_reader()
    reader._read.return_value = []
    reader.styles(config()[1]["catalog"], ["quoted' style"])
    sql, params = reader._read.call_args.args
    assert "SELECT DISTINCT" in sql and "`labels` AS `labels`" in sql
    assert "quoted" not in sql and params[-1] == "quoted' style"


def test_sku_cannot_be_allocated_to_two_requested_styles():
    reader = fake_reader()
    reader.styles.return_value.append({**reader.styles.return_value[0], "style": "KQ25002"})
    result = inspect_forecast(reader, ["KQ25001", "KQ25002"], date(2026, 9, 12), *config())
    assert all("sku_assigned_to_multiple_styles" in g["issues"] for g in result["groups"])
    reader.sales_window.assert_not_called()


def test_source_missing_day_keeps_forecast_empty_in_report(tmp_path):
    reader = fake_reader()
    reader.daily_window.side_effect = None
    reader.daily_window.return_value = {"rows": [], "issues": ["daily_source_needs_review"]}
    report = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *config())
    write_forecast_report(tmp_path, report)
    with (tmp_path / "forecast.csv").open(encoding="utf-8-sig") as stream:
        rows = [row for row in csv.DictReader(stream) if row["platform"] == "jd_self"]
    assert all(row["forecast"] == "" and row["issues"] for row in rows)


def test_requested_rows_query_only_sheet_skus_and_ignore_outside_season_conflict():
    reader = fake_reader()
    reader.styles.return_value.append(
        {
            "sku": "outside",
            "style": "KQ25001",
            "name": "表外商品",
            "spec": "表外规格",
            "labels": "秋冬款",
        }
    )
    requested = [
        {"row": i + 4, "issues": [], **row} for i, row in enumerate(reader.styles.return_value[:2])
    ]
    result = inspect_forecast(
        reader,
        [],
        date(2026, 9, 12),
        *config(),
        requested_rows=requested,
    )
    reader.styles.assert_not_called()
    assert reader.catalog.call_args.args[1] == ["001", "002"]
    assert result["mode"] == "read_only_requested_skus"
    assert result["summary"]["ready"] == 5
    assert result["groups"][0]["skus"] == ["001", "002"]
    for call in [*reader.sales_window.call_args_list, *reader.daily_window.call_args_list]:
        assert call.args[1] == ["001", "002"]
    assert all(not row["issues"] for row in requested)


@pytest.mark.parametrize("problem", ["missing", "wrong_style", "ambiguous"])
def test_requested_sku_lookup_problems_remain_visible(problem):
    reader = fake_reader()
    requested = [{"row": 4, "issues": [], **reader.styles.return_value[0]}]
    if problem == "missing":
        reader.styles.return_value.pop(0)
    elif problem == "wrong_style":
        reader.styles.return_value[0]["style"] = "KQ25002"
    else:
        reader.styles.return_value.append({**reader.styles.return_value[0], "spec": "不同规格"})
    result = inspect_forecast(reader, [], date(2026, 9, 12), *config(), requested_rows=requested)
    assert result["groups"][0]["status"] == "needs_review"
    assert result["groups"][0]["issues"]
    reader.sales_window.assert_not_called()
    reader.daily_window.assert_not_called()


@pytest.mark.parametrize("days", [7, 14, 28, 30, 60, 90])
def test_fixed_window_reads_only_end_snapshot(days):
    reader, _, start, _ = daily_reader()
    source = config()[1]["daily"]["jd_self"]
    reader._read.return_value = [{"sku": "001", "row_id": "jd", "quantity": 59}]
    stop = start + timedelta(days=days)
    result = reader.daily_window(source, ["001"], start, stop)
    assert result["kind"] == "rolling" and not result["issues"]
    assert result["rows"][0]["quantity"] == 59
    sql, params = reader._read.call_args.args
    assert f"outbound_{days}d" in sql and "outbound_yesterday" not in sql
    assert params[-3:-1] == (str(stop - timedelta(days=1)), str(stop))


@pytest.mark.parametrize(
    "records,reason",
    [
        ([], "missing_rolling_snapshot"),
        ([{"sku": "001", "row_id": "jd", "quantity": 2}] * 2, "duplicate_source_sku_day"),
        ([{"sku": "001", "row_id": "jd", "quantity": None}], "invalid_rolling_value"),
    ],
)
def test_fixed_snapshot_invalid_is_not_zero(records, reason):
    reader, _, start, _ = daily_reader()
    reader._read.return_value = records
    result = reader.daily_window(
        config()[1]["daily"]["jd_self"], ["001"], start, start + timedelta(days=30)
    )
    assert reason in result["rows"][0]["issues"]
    assert result["rows"][0]["quantity"] is None


def test_zero_previous_requires_manual_forecast():
    reader = fake_reader()
    original = reader.sales_window.side_effect

    def window(source, skus, start, stop):
        result = original(source, skus, start, stop)
        if start.year == 2025 and (stop - start).days == 30:
            for row in result["rows"]:
                row["quantity"] = 0
        return result

    reader.sales_window.side_effect = window
    result = inspect_forecast(reader, ["KQ25001"], date(2026, 9, 12), *config())
    assert result["summary"]["manual"] == 4
    assert all(
        p["status"] == "manual" and "forecast" not in p
        for p in result["groups"][0]["platforms"]
        if p["platform"] != "jd_self"
    )


def test_arbitrary_142_day_window_sums_daily_values():
    reader, _, start, _ = daily_reader()
    source = config()[1]["daily"]["jd_self"]
    reader._read.return_value = [
        {
            "sku": "001",
            "date": start + timedelta(days=n),
            "row_id": "jd",
            "quantity": 2,
            "rolling_30": 999,
        }
        for n in range(142)
    ]
    result = reader.daily_window(source, ["001"], start, start + timedelta(days=142))
    assert result["kind"] == "daily" and not result["issues"]
    assert result["rows"][0]["quantity"] == 284


def test_platform_log_uses_names_and_does_not_claim_written(caplog):
    import logging

    from stocking_sheet_sync.services.calculation import log_platform_result

    with caplog.at_level(logging.INFO):
        log_platform_result(
            "款号",
            "拼多多",
            {
                "platform": "pdd",
                "status": "manual",
                "inputs": {"current": {"a": 18}},
                "issues": ["company_lifecycle_sales_unavailable"],
            },
        )
        log_platform_result(
            "款号",
            "唯品会",
            {
                "platform": "vip",
                "status": "ready",
                "inputs": {"current": {"a": 22}},
                "forecast": {"total": 2100},
                "issues": [],
            },
        )
        log_platform_result(
            "款号",
            "京东自营",
            {
                "platform": "jd_self",
                "status": "needs_review",
                "inputs": {"previous": {}, "historical_future": {}},
                "issues": ["rolling_source_needs_review"],
            },
        )
    assert "拼多多：历史数据可用（近30天18件）" in caplog.text
    assert "表内全公司销量不可用" in caplog.text
    assert "预估2100件，待写入" in caplog.text
    assert "原因：近30天数据缺失或校验未通过" in caplog.text
    assert "company_lifecycle_sales_unavailable" not in caplog.text
    assert "needs_review" not in caplog.text

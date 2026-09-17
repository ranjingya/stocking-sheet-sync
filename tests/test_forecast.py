from datetime import date

import pytest

from stocking_sheet_sync.forecast import allocate_forecast, forecast_window, load_forecast_config


@pytest.mark.parametrize(
    "day,label,end",
    [
        ("2026-09-17", "秋冬款", "2027-01-31"),
        ("2026-01-20", "秋冬款", "2026-01-31"),
        ("2026-04-20", "夏款", "2026-08-31"),
        ("2026-01-31", "四季款", "2026-01-31"),
        ("2026-02-01", None, "2026-08-31"),
        ("2026-08-31", "公司款", "2026-08-31"),
        ("2026-09-01", "", "2027-01-31"),
    ],
)
def test_season_deadlines(day, label, end):
    result = forecast_window(date.fromisoformat(day), [label], load_forecast_config())
    assert result["deadline"].isoformat() == end
    assert (result["current_end"] - result["current_start"]).days == 30
    assert (result["previous_end"] - result["previous_start"]).days == 30


def test_history_window_and_leap_day():
    r = forecast_window(date(2026, 9, 17), ["秋冬款"], load_forecast_config())
    assert r["history_start"] == date(2025, 9, 17)
    assert r["history_end"] == date(2026, 2, 1)
    leap = forecast_window(date(2024, 2, 29), [None], load_forecast_config())
    assert leap["previous_end"] == date(2023, 2, 28)
    assert (leap["previous_end"] - leap["previous_start"]).days == 30


@pytest.mark.parametrize("labels", [["夏款,秋冬款"], ["夏款", "四季款"], ["秋冬款", None]])
def test_conflicting_labels_are_not_arbitrarily_selected(labels):
    with pytest.raises(ValueError):
        forecast_window(date(2026, 5, 1), labels, load_forecast_config())


def test_out_of_season_does_not_silently_extend_year():
    with pytest.raises(ValueError, match="跨季"):
        forecast_window(date(2026, 9, 1), ["夏款"], load_forecast_config())


def test_platform_growth_and_decline():
    r = allocate_forecast({"a": 30, "b": 10}, 80, 100, None, load_forecast_config())
    assert r["total"] == 50
    assert r["rows"]["a"]["rounded"] == 38
    assert r["rows"]["b"]["rounded"] == 13
    assert r["rows"]["a"]["quantity"] == 37
    assert r["rows"]["b"]["quantity"] == 13
    assert r["rounding_difference"] == -1


def test_company_fallback_only_changes_shares():
    r = allocate_forecast({"a": 10, "b": 0}, 20, 100, {"a": 20, "b": 80}, load_forecast_config())
    assert r["total"] == 50 and r["share_source"] == "company"
    assert [r["rows"][k]["quantity"] for k in ["a", "b"]] == [10, 40]


@pytest.mark.parametrize("current", [{"a": 20}, {str(n): 1 for n in range(10)}])
def test_thresholds_are_strictly_less(current):
    assert (
        allocate_forecast(current, 10, 100, None, load_forecast_config())["share_source"]
        == "platform"
    )


@pytest.mark.parametrize("previous,company", [(0, {"a": 1}), (10, None), (10, {"a": 0})])
def test_unavailable_denominators_are_not_zero_forecasts(previous, company):
    with pytest.raises(ValueError):
        allocate_forecast({"a": 1}, previous, 10, company, load_forecast_config())


def test_rounding_never_goes_negative_and_matches_total():
    rules = load_forecast_config()
    for n in range(1, 20):
        for historical in range(30):
            current = {str(i): 20 for i in range(n)}
            r = allocate_forecast(current, 100, historical, None, rules)
            assert sum(v["quantity"] for v in r["rows"].values()) == r["total"]
            assert all(v["quantity"] >= 0 for v in r["rows"].values())


def test_arbitrary_detail_window_checks_all_dates():
    from stocking_sheet_sync.sales_reader import SalesReader

    class Reader(SalesReader):
        def _read(self, sql, params):
            if "AS latest" in sql:
                return [{"latest": "2025-10-31"}]
            if "GROUP BY DATE" in sql:
                return [{"day": "2025-09-01", "n": 1}]
            return []

    source = {
        "id": "test",
        "kind": "detail",
        "table": "facts",
        "fields": {"sku": "sku", "date": "day", "quantity": "qty"},
        "unique_fields": ["id", "sku"],
    }
    result = Reader(None).sales_window(source, ["sku"], date(2025, 9, 1), date(2025, 11, 1))
    assert result["start"] == "2025-09-01" and result["end"] == "2025-10-31"
    assert len(result["missing_dates"]) == 60
    assert result["issues"] == ["incomplete_daily_coverage"]


def test_rolling_snapshot_rejects_season_sum():
    from stocking_sheet_sync.sales_reader import SalesReader

    with pytest.raises(ValueError, match="滚动"):
        SalesReader(None).sales_window(
            {"kind": "snapshot"}, ["sku"], date(2025, 9, 1), date(2026, 2, 1)
        )

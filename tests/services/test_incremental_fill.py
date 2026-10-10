from datetime import date

import pytest

from stocking_sheet_sync.domain.products import inspect_sheet
from stocking_sheet_sync.domain.sheets.forecast_values import build_forecast_values
from stocking_sheet_sync.domain.sheets.layout import plan_market_layout
from stocking_sheet_sync.domain.sheets.validation import verify_sales_update
from stocking_sheet_sync.services.calculation import inspect_forecast
from stocking_sheet_sync.services.notification import summarize_platform_fill
from tests.services.test_forecast_inspect import config as configs
from tests.services.test_forecast_sheet import filled, setup_sheet


def scenario():
    _, reader, report, _, snapshot = setup_sheet()
    sales, sources, forecast, layout = configs()
    sales["incremental"] = True
    forecast["previous_sales_below"] = 10
    update = build_forecast_values(snapshot, report, sales, layout, history=True, forecast=True)
    snapshot = filled(snapshot, update)
    snapshot["cells"]["A6"]["value"] = "KQ25002"
    reader.styles.return_value[1]["style"] = "KQ25002"
    fields = {
        (f["platform"], f["metric"]): f["target_column"]
        for f in plan_market_layout(snapshot, sales, layout, recent_only=True, forecast=True)[
            "target_fields"
        ]
    }
    for (_pid, metric), col in fields.items():
        if metric != "demand":
            snapshot["cells"][f"{col}6"] = {}
    reader.reset_mock()
    return snapshot, reader, (sales, sources, forecast, layout), fields


def calculate(snapshot, reader, cfg):
    return inspect_forecast(
        reader,
        [],
        date(2026, 9, 12),
        *cfg,
        requested_rows=inspect_sheet(snapshot, cfg[0])["rows"],
        snapshot=snapshot,
    )


def test_complete_old_style_is_not_queried_and_new_style_fills(caplog):
    snapshot, reader, cfg, fields = scenario()
    with caplog.at_level("INFO"):
        report = calculate(snapshot, reader, cfg)
    assert "已完整跳过" in caplog.text and "首次填充" in caplog.text
    for call in reader.daily_window.call_args_list + reader.sales_window.call_args_list:
        assert call.args[1] == [reader.styles.return_value[1]["sku"]]
    update = build_forecast_values(
        snapshot, report, cfg[0], cfg[3], history=True, forecast=True, partial=True
    )
    assert not update["blocked_scopes"]
    assert {e["row"] for e in update["entries"]} == {6}
    assert len(update["entries"]) == 20
    assert verify_sales_update(snapshot, filled(snapshot, update), update)["verified"]


def test_conflict_in_old_style_does_not_block_new_style_same_platform():
    snapshot, reader, cfg, fields = scenario()
    snapshot["cells"][fields[("pdd", "forecast")] + "4"] = {}
    snapshot["cells"][fields[("pdd", "sales")] + "4"] = {"value": 999}
    report = calculate(snapshot, reader, cfg)
    update = build_forecast_values(
        snapshot, report, cfg[0], cfg[3], history=True, forecast=True, partial=True
    )
    assert {(e["style"], e["platform"]) for e in update["blocked_scopes"]} == {("KQ25001", "pdd")}
    assert any(e["platform"] == "pdd:forecast" and e["row"] == 6 for e in update["entries"])
    assert {e["metric"] for e in update["entries"] if e["row"] == 4} == {"previous", "future"}
    assert verify_sales_update(snapshot, filled(snapshot, update), update)["verified"]
    details = summarize_platform_fill(update, cfg[0], history=True, report=report)
    assert details["history"]["status"] == "partial"
    assert any("KQ25001" in r for r in details["history"]["reasons"])


def test_same_style_new_sku_calculates_whole_style_preserves_old_forecast():
    snapshot, reader, cfg, fields = scenario()
    snapshot["cells"]["A6"]["value"] = "KQ25001"
    reader.styles.return_value[1]["style"] = "KQ25001"
    old = fields[("vip", "forecast")] + "4"
    snapshot["cells"][old] = {"value": 12345}
    report = calculate(snapshot, reader, cfg)
    assert all(len(c.args[1]) == 2 for c in reader.sales_window.call_args_list)
    update = build_forecast_values(
        snapshot, report, cfg[0], cfg[3], history=True, forecast=True, partial=True
    )
    assert old not in {e["target_cell"] for e in update["entries"]}
    after = filled(snapshot, update)
    assert after["cells"][old]["value"] == 12345
    assert verify_sales_update(snapshot, after, update)["verified"]


@pytest.mark.parametrize("forecast_only", [True, False])
def test_history_conflict_blocks_forecast_even_when_only_requesting_forecast(forecast_only):
    snapshot, reader, cfg, fields = scenario()
    snapshot["cells"][fields[("vip", "sales")] + "6"] = {"value": 999}
    report = calculate(snapshot, reader, cfg)
    update = build_forecast_values(
        snapshot, report, cfg[0], cfg[3], history=not forecast_only, forecast=True, partial=True
    )
    assert not any(e["platform"] in {"vip:sales", "vip:forecast"} for e in update["entries"])
    assert any(e["platform"] == "pdd:forecast" for e in update["entries"])


def test_overwrite_recalculates_complete_scopes():
    snapshot, reader, cfg, fields = scenario()
    cfg[0]["overwrite"] = True
    report = calculate(snapshot, reader, cfg)
    assert all(not p.get("preserved") for g in report["groups"] for p in g["platforms"])
    update = build_forecast_values(
        snapshot, report, cfg[0], cfg[3], history=True, forecast=True, partial=True
    )
    assert {e["row"] for e in update["entries"]} == {4, 6}


def test_history_only_complete_style_skips_and_other_style_fills():
    from stocking_sheet_sync.domain.sheets.values import build_sales_update
    from stocking_sheet_sync.services.sales import inspect_sales

    snapshot, reader, cfg, fields = scenario()
    reader.sales.side_effect = lambda p, skus, day: {
        "platform": p["id"],
        "issues": [],
        "rows": [{"sku": sku, "quantity": 20, "status": "matched"} for sku in skus],
    }
    report = inspect_sales(reader, snapshot, cfg[0], date(2026, 9, 12))
    assert all(len(c.args[1]) == 1 for c in reader.sales.call_args_list)
    update = build_sales_update(snapshot, report, cfg[0], partial=True)
    assert {e["row"] for e in update["entries"]} == {6}
    assert verify_sales_update(snapshot, filled(snapshot, update), update)["verified"]


def test_incremental_totals_include_preserved_old_values():
    snapshot, reader, cfg, fields = scenario()
    for metric in ("sales", "previous", "future", "forecast", "demand"):
        col = fields[("vip", metric)]
        snapshot["cells"][f"{col}7"] = {
            "formula": f"=SUM({col}4:{col}6)",
            "value": snapshot["cells"][f"{col}4"].get("value", 0),
        }
    report = calculate(snapshot, reader, cfg)
    update = build_forecast_values(
        snapshot, report, cfg[0], cfg[3], history=True, forecast=True, partial=True
    )
    total = next(t for t in update["total_entries"] if t["platform"] == "vip:sales")
    assert total["expected_quantity"] == 80
    assert verify_sales_update(snapshot, filled(snapshot, update), update)["verified"]


def test_different_as_of_blocks_new_rows_under_existing_dated_column():
    snapshot, reader, cfg, fields = scenario()
    for (pid, metric), col in fields.items():
        if metric == "future":
            snapshot["cells"][f"{col}3"]["value"] = (
                cfg[3]["platforms"][pid]["future_prefix"] + "25.9.11-26.1.31"
            )
    report = calculate(snapshot, reader, cfg)
    assert all(
        p["history_conflicts"]
        for g in report["groups"]
        if g["style"] == "KQ25002"
        for p in g["platforms"]
    )


def test_zero_is_complete_and_history_gap_is_supplement(caplog):
    from stocking_sheet_sync.domain.sheets.incremental import fill_state

    snapshot, reader, cfg, fields = scenario()
    rows = [r for r in inspect_sheet(snapshot, cfg[0])["rows"] if r["row"] == 4]
    columns = {metric: col for (pid, metric), col in fields.items() if pid == "vip"}
    for col in columns.values():
        snapshot["cells"][col + "4"] = {"value": 0}
    assert fill_state(snapshot, rows, columns, forecast=True, overwrite=False) == "已完整跳过"
    snapshot["cells"][columns["forecast"] + "4"] = {}
    assert fill_state(snapshot, rows, columns, forecast=True, overwrite=False) == "补预估"
    snapshot["cells"][columns["previous"] + "4"] = {}
    assert fill_state(snapshot, rows, columns, forecast=True, overwrite=False) == "补历史并检查预估"


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("missing_key", ["current", "previous", "historical_future"])
def test_automatic_fill_isolates_style_and_history_metric(incremental, missing_key):
    """自动填充保留失败款可用历史、其他款全部指标，并准确汇总通知。"""
    snapshot, reader, cfg, fields = scenario()
    sales, sources, forecast, layout = cfg
    sales["incremental"] = incremental
    for (_pid, metric), col in fields.items():
        if metric != "demand":
            for row in (4, 6):
                snapshot["cells"][f"{col}{row}"] = {}
    report = calculate(snapshot, reader, cfg)
    affected = next(
        p for p in report["groups"][0]["platforms"] if p["platform"] == "tmall_supermarket"
    )
    affected["inputs"].pop(missing_key)
    affected.update(
        status="needs_review", issues=[f"{missing_key}: 数据来源读取失败（错误码 1064）"]
    )
    update = build_forecast_values(
        snapshot, report, sales, layout, history=True, forecast=True, partial=True
    )
    style = report["groups"][0]["style"]
    entries = [e for e in update["entries"] if e["platform"].startswith("tmall_supermarket:")]
    assert {e["metric"] for e in entries if e["style"] == style} == (
        {"sales", "previous", "future"}
        - {{"current": "sales", "previous": "previous", "historical_future": "future"}[missing_key]}
    )
    assert {e["metric"] for e in entries if e["style"] != style} == {
        "sales",
        "previous",
        "future",
        "forecast",
    }
    after = filled(snapshot, update)
    assert verify_sales_update(snapshot, after, update)["verified"]
    details = summarize_platform_fill(update, sales, history=True, report=report)
    assert details["history"]["completed"] == 4
    assert details["forecast"]["completed"] == 4
    assert any("1064" in reason for reason in details["history"]["reasons"])

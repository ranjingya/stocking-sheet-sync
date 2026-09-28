from copy import deepcopy
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.domain.models import CopyState, FillState
from stocking_sheet_sync.domain.products import inspect_sheet
from stocking_sheet_sync.domain.sheets.company import append_company_values
from stocking_sheet_sync.domain.sheets.forecast_values import build_forecast_values, project_layout
from stocking_sheet_sync.domain.sheets.layout import build_update, dated_forecast_rules
from stocking_sheet_sync.domain.sheets.validation import verify_sales_update, verify_update
from stocking_sheet_sync.services.calculation import inspect_forecast
from stocking_sheet_sync.services.company import resolve_company_sales
from stocking_sheet_sync.services.fill import ForecastFiller, HistoryFiller, remap_report
from stocking_sheet_sync.services.sales import inspect_sales
from tests.domain.test_company_sales import company_sheet, requested
from tests.services.test_forecast_inspect import config as configs
from tests.services.test_forecast_inspect import fake_reader
from tests.services.test_forecast_sheet import config, filled, rules, setup_sheet


def loader(skus, start, stop):
    assert start == date(2025, 9, 12)
    assert stop == date(2026, 2, 1)
    return [{"sku": sku, "quantity": 10 + i * 20, "issues": []} for i, sku in enumerate(skus)]


def test_existing_manual_company_keeps_period_and_never_queries():
    load = Mock(side_effect=AssertionError("已有完整人工列不能查询"))
    result = resolve_company_sales(
        company_sheet(), requested(), [], date(2026, 9, 12), configs()[2], load
    )
    assert result["period"] == "25.9.1-26.1.31"
    assert not result.get("automatic")
    load.assert_not_called()


@pytest.mark.parametrize("history_only", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_company_fill_pipeline_inserts_first_preserves_formulas_and_isolates_failure(
    tmp_path, monkeypatch, history_only, failure
):
    before, reader, _, _, _ = setup_sheet()
    before["cells"]["E7"] = {"formula": "=SUM(F4:F6)", "value": 100}
    rows = inspect_sheet(before, config())["rows"]

    def load(skus, start, stop):
        result = loader(skus, start, stop)
        if failure:
            result[0].update(quantity=None, issues=["京东自营后续周期出库缺失或不完整"])
        return result

    if history_only:
        reader.sales.side_effect = lambda src, skus, as_of: {
            "platform": src["id"],
            "issues": [],
            "rows": [{"sku": sku, "quantity": 40, "status": "matched"} for sku in skus],
        }
        report = inspect_sales(reader, before, config(), date(2026, 9, 12))
        company = resolve_company_sales(
            before, rows, report["catalog"], date(2026, 9, 12), configs()[2], load
        )
        custom_rules = rules()
        cls = HistoryFiller
    else:
        report = inspect_forecast(
            reader,
            [],
            date(2026, 9, 12),
            *configs(),
            requested_rows=rows,
            snapshot=before,
            company_window=load,
        )
        company = report["company_source"]
        custom_rules = dated_forecast_rules(rules(), report)
        cls = ForecastFiller
    custom_rules["company_source"] = company
    layout = build_update(before, config(), custom_rules, forecast=not history_only)
    prepared = project_layout(before, layout, config(), custom_rules)
    # 内存投影不执行电子表格引擎，模拟插列后公式引用自动右移。
    prepared["cells"]["E7"]["formula"] = "=SUM(H4:H6)"
    assert layout["report"]["target_fields"][0]["platform"] == "company"
    assert prepared["cells"]["F3"]["value"] == "全公司出库 25.9.12-26.1.31"
    assert verify_update(before, prepared, layout, config(), custom_rules)["verified"]
    if history_only:
        from stocking_sheet_sync.domain.sheets.values import build_sales_update

        update = build_sales_update(
            prepared, remap_report(report, prepared, config()), config(), partial=True
        )
    else:
        update = build_forecast_values(
            prepared, report, config(), custom_rules, history=True, forecast=True, partial=True
        )
    update = append_company_values(prepared, update, company, config())
    after = filled(prepared, update)
    assert verify_sales_update(prepared, after, update)["verified"]
    assert after["cells"]["E7"]["formula"] == "=SUM(H4:H6)"
    assert after["cells"]["F4"].get("value") == (None if failure else 10)
    assert after["cells"]["F6"]["value"] == 30
    assert (
        build_update(after, config(), custom_rules, forecast=not history_only)["operations"] == []
    )
    filler = cls(
        object(),
        config_path=Path("config/config.example.toml"),
        reader_factory=lambda: reader,
        **({} if history_only else {"history": True}),
    )
    monkeypatch.setattr(filler, "company_window", lambda *a: load)
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    reads = iter([before, prepared, after])
    monkeypatch.setattr("stocking_sheet_sync.services.fill.read_sheet", lambda *a, **k: next(reads))
    apply = Mock(return_value={})
    write = Mock(return_value={})
    monkeypatch.setattr("stocking_sheet_sync.services.fill.apply_update", apply)
    monkeypatch.setattr("stocking_sheet_sync.services.fill.write_sales_ranges", write)
    result = filler(
        CopyState("r", "s", "name", "url", "record", "copied", target_token="test-token"),
        FillState("r", "s", "test-token", "2026-09-12", "a", report_path=str(tmp_path)),
    )
    assert result["status"] == "completed", result
    assert result["notification_details"]["history"]["status"] == "completed"
    assert bool(result["notification_details"].get("company")) == failure
    assert apply.call_count == write.call_count == 1


def generated_sheet(values=(10, 30), period="25.9.12-26.1.31"):
    snapshot = company_sheet(values)
    snapshot["cells"]["C1"] = {}
    snapshot["cells"]["C3"] = {"value": "全公司出库 " + period}
    return snapshot


def test_generated_same_period_idempotent_and_stale_period_refused():
    load = Mock(side_effect=AssertionError("不应查询"))
    rows = requested()
    catalog = fake_reader().styles.return_value
    same = resolve_company_sales(
        generated_sheet(), rows, catalog, date(2026, 9, 12), configs()[2], load
    )
    assert same["status"] == "available" and not same.get("automatic")
    stale = resolve_company_sales(
        generated_sheet(), rows, catalog, date(2026, 9, 13), configs()[2], load
    )
    assert stale["status"] == "unavailable" and not stale.get("automatic")
    assert "日期" in stale["reasons"][0]
    load.assert_not_called()


@pytest.mark.parametrize("overwrite", [False, True])
def test_partial_existing_generated_values_conflict_does_not_sneak_into_forecast(overwrite):
    result = resolve_company_sales(
        generated_sheet((999, None)),
        requested(),
        fake_reader().styles.return_value,
        date(2026, 9, 12),
        configs()[2],
        loader,
        overwrite=overwrite,
    )
    assert [r["quantity"] for r in result["rows"]] == ([10, 30] if overwrite else [None, 30])
    assert result["status"] == ("available" if overwrite else "partial")


def test_partial_manual_gets_separate_column_without_mixing_periods():
    sheet = company_sheet((10, None))
    original = deepcopy(sheet)
    result = resolve_company_sales(
        sheet,
        requested(),
        fake_reader().styles.return_value,
        date(2026, 9, 12),
        configs()[2],
        loader,
    )
    assert result["automatic"] and result["header"] == "全公司出库 25.9.12-26.1.31"
    assert [r["quantity"] for r in result["rows"]] == [10, 30]
    assert sheet == original


def test_fallback_uses_auto_company_share_and_missing_company_only_blocks_fallback():
    before, reader, _, _, _ = setup_sheet()
    rows = inspect_sheet(before, config())["rows"]
    original = reader.sales_window.side_effect

    def window(source, skus, start, stop):
        result = original(source, skus, start, stop)
        if source["id"] == "pdd" and start.year == 2026:
            for row in result["rows"]:
                row["quantity"] = 2
        return result

    reader.sales_window.side_effect = window
    for load, expected in [(loader, "ready"), (lambda *a: [], "manual")]:
        report = inspect_forecast(
            reader,
            [],
            date(2026, 9, 12),
            *configs(),
            requested_rows=rows,
            snapshot=before,
            company_window=load,
        )
        platforms = {p["platform"]: p for p in report["groups"][0]["platforms"]}
        assert platforms["pdd"]["status"] == expected
        assert platforms["vip"]["status"] == "ready"
        assert len(platforms["pdd"]["inputs"]) == 3
        if expected == "ready":
            assert platforms["pdd"]["forecast"]["share_source"] == "company"
            assert {
                sku: r["quantity"] for sku, r in platforms["pdd"]["forecast"]["rows"].items()
            } == {row["sku"]: row["quantity"] for row in report["company_source"]["rows"]}


def test_force_date_change_requires_all_rows_before_relabel():
    def load(skus, start, stop):
        return [{"sku": sku, "quantity": None, "issues": ["京东缺日"]} for sku in skus]

    result = resolve_company_sales(
        generated_sheet(),
        requested(),
        fake_reader().styles.return_value,
        date(2026, 9, 13),
        configs()[2],
        load,
        overwrite=True,
    )
    assert result["status"] == "unavailable" and not result["automatic"]
    assert result["rows"] == []
    assert "保留原周期列" in result["reasons"][-1]


def test_company_source_error_does_not_raise_and_provides_reason():
    load = Mock(side_effect=RuntimeError("店铺读取无权限"))
    result = resolve_company_sales(
        company_sheet((None, None)),
        requested(),
        fake_reader().styles.return_value,
        date(2026, 9, 12),
        configs()[2],
        load,
    )
    assert result["automatic"] and result["status"] == "partial"
    assert all(r["quantity"] is None for r in result["rows"])
    assert "店铺读取无权限" in result["reasons"][0]


def test_partial_company_notifies_without_marking_valid_platform_history_unfilled():
    from stocking_sheet_sync.services.notification import build_sync_card, summarize_platform_fill

    _, _, report, _, _ = setup_sheet()
    report["company_source"] = {"reasons": ["全公司：京东出库缺失"]}
    details = summarize_platform_fill({}, config(), history=True, report=report)
    assert details["history"]["status"] == details["forecast"]["status"] == "completed"
    card = build_sync_card(
        original_name="需求表",
        record_url="https://example.com/source",
        status="success",
        target_url="https://example.com/result",
        details=details,
    )
    assert card["header"]["title"]["content"] == "下单需求 · 部分完成"
    assert "全公司：京东出库缺失" in card["body"]["elements"][1]["content"]
    assert card["body"]["elements"][-1]["tag"] == "column_set"


def test_company_can_fill_when_all_platforms_are_blocked():
    before, reader, report, _, _ = setup_sheet()
    rows = inspect_sheet(before, config())["rows"]
    company = resolve_company_sales(
        before, rows, reader.styles.return_value, date(2026, 9, 12), configs()[2], loader
    )
    custom = dated_forecast_rules(rules(), report)
    custom["company_source"] = company
    layout = build_update(before, config(), custom, forecast=True)
    prepared = project_layout(before, layout, config(), custom)
    for platform in report["groups"][0]["platforms"]:
        platform.update(status="needs_review", inputs={}, issues=["source_read_failed"])
    update = build_forecast_values(
        prepared, report, config(), custom, history=True, forecast=True, partial=True
    )
    assert update["summary"]["needs_review"]
    update = append_company_values(prepared, update, company, config())
    assert not update["summary"]["needs_review"]
    assert len(update["blocked_platforms"]) == 5
    assert {e["platform"] for e in update["entries"]} == {"company:sales"}
    assert verify_sales_update(prepared, filled(prepared, update), update)["verified"]

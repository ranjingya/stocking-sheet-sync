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
    assert start == date(2026, 8, 13)
    assert stop == date(2026, 9, 12)
    return [{"sku": sku, "quantity": 10 + i * 20, "issues": []} for i, sku in enumerate(skus)]


def test_old_lifecycle_column_is_preserved_but_queries_recent_sales():
    sheet = company_sheet()
    sheet["cells"]["C3"] = {"value": "25.9.1-26.1.31"}
    before = deepcopy(sheet)
    load = Mock(side_effect=loader)
    result = resolve_company_sales(
        sheet, requested(), fake_reader().styles.return_value, date(2026, 9, 12), configs()[2], load
    )
    assert result["header"] == "全公司近30天出库 26.8.13-26.9.11"
    assert result["automatic"]
    load.assert_called_once()
    assert sheet == before


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
            result[0].update(quantity=None, issues=["京东自营近30天出库缺失或不完整"])
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
    if failure:
        assert not company["automatic"]
        assert all(row["quantity"] is None for row in company["rows"])
        assert all(field["platform"] != "company" for field in layout["report"]["target_fields"])
        return
    prepared = project_layout(before, layout, config(), custom_rules)
    # 内存投影不执行电子表格引擎，模拟插列后公式引用自动右移。
    prepared["cells"]["E7"]["formula"] = "=SUM(H4:H6)"
    assert layout["report"]["target_fields"][0]["platform"] == "company"
    assert prepared["cells"]["F3"]["value"] == "全公司近30天出库 26.8.13-26.9.11"
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


def generated_sheet(values=(10, 30), period="26.8.13-26.9.11"):
    snapshot = company_sheet(values)
    snapshot["cells"]["C1"] = {}
    snapshot["cells"]["C3"] = {"value": "全公司近30天出库 " + period}
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
    assert [r["quantity"] for r in result["rows"]] == ([10, 30] if overwrite else [None, None])
    assert result["status"] == ("available" if overwrite else "partial")


def test_partial_manual_gets_separate_column_without_mixing_periods():
    sheet = company_sheet((10, None))
    sheet["cells"]["C3"] = {"value": "25.9.1-26.1.31"}
    original = deepcopy(sheet)
    result = resolve_company_sales(
        sheet,
        requested(),
        fake_reader().styles.return_value,
        date(2026, 9, 12),
        configs()[2],
        loader,
    )
    assert result["automatic"] and result["header"] == "全公司近30天出库 26.8.13-26.9.11"
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
            forecast = platforms["pdd"]["forecast"]
            from decimal import Decimal

            assert Decimal(forecast["unrounded_total"]) == (
                Decimal(forecast["historical_future"])
                * forecast["current_total"]
                / forecast["previous_total"]
            )
            assert {
                sku: r["quantity"] for sku, r in platforms["pdd"]["forecast"]["rows"].items()
            } == {row["sku"]: row["quantity"] for row in report["company_source"]["rows"]}


@pytest.mark.parametrize(
    "as_of,start",
    [
        (date(2026, 10, 6), date(2026, 9, 6)),
        (date(2026, 1, 10), date(2025, 12, 11)),
        (date(2024, 3, 1), date(2024, 1, 31)),
    ],
)
def test_recent_window_is_thirty_complete_days_independent_of_season(as_of, start):
    """跨月、跨年及闰年的公司窗口均不含基准日，且不依赖季节。"""
    sheet = company_sheet()
    sheet["cells"]["C3"] = {"value": "全公司出库 25.9.1-26.1.31"}
    catalog = deepcopy(fake_reader().styles.return_value)
    for item in catalog:
        item["labels"] = []
    load = Mock(
        return_value=[{"sku": row["sku"], "quantity": 10, "issues": []} for row in requested()]
    )
    result = resolve_company_sales(sheet, requested(), catalog, as_of, configs()[2], load)
    assert result["status"] == "available"
    load.assert_called_once_with(sorted(row["sku"] for row in requested()), start, as_of)


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
    sheet = company_sheet((None, None))
    sheet["cells"]["C3"] = {"value": "25.9.1-26.1.31"}
    result = resolve_company_sales(
        sheet,
        requested(),
        fake_reader().styles.return_value,
        date(2026, 9, 12),
        configs()[2],
        load,
    )
    assert not result["automatic"] and result["status"] == "unavailable"
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
    assert len({s["platform"] for s in update["blocked_scopes"]}) == 5
    assert {e["platform"] for e in update["entries"]} == {"company:sales"}
    assert verify_sales_update(prepared, filled(prepared, update), update)["verified"]


@pytest.mark.parametrize("quantity", [None, 0, 10])
def test_company_column_requires_complete_style(quantity):
    """整款完整时新增公司列，有效零值也属于完整数据。"""
    before, reader, _, _, _ = setup_sheet()
    rows = inspect_sheet(before, config())["rows"]

    def load(skus, start, stop):
        return [{"sku": sku, "quantity": quantity, "issues": []} for i, sku in enumerate(skus)]

    company = resolve_company_sales(
        before, rows, reader.styles.return_value, date(2026, 9, 12), configs()[2], load
    )
    custom = rules()
    custom["company_source"] = company
    layout = build_update(before, config(), custom, forecast=True)
    fields = layout["report"]["target_fields"]
    assert any(field["platform"] == "company" for field in fields) == (quantity is not None)
    assert company["automatic"] == (quantity is not None)
    if quantity is None:
        assert "全公司：所有款数据均不完整，不新增出库列" in company["reasons"]


@pytest.mark.parametrize("second_complete", [False, True])
def test_company_incomplete_style_is_blank_without_blocking_other_style(second_complete):
    """任一SKU缺失则整款留空，仅有完整款时新增列。"""
    before, reader, _, _, _ = setup_sheet()
    rows = inspect_sheet(before, config())["rows"]
    catalog = deepcopy(reader.styles.return_value)
    extra = []
    for index, row in enumerate(rows):
        item = {**row, "style": "OTHER_STYLE", "sku": "extra_" + row["sku"], "row": 20 + index}
        extra.append(item)
    catalog.extend({**item, "labels": catalog[0].get("labels")} for item in extra)
    first_sku = rows[0]["sku"]

    def load(skus, start, stop):
        return [
            {
                "sku": sku,
                "quantity": None
                if sku == first_sku or (sku.startswith("extra_") and not second_complete)
                else 0,
                "issues": [],
            }
            for sku in skus
        ]

    company = resolve_company_sales(
        before, rows + extra, catalog, date(2026, 9, 12), configs()[2], load
    )
    assert all(
        item["quantity"] is None for item in company["rows"] if item["style"] != "OTHER_STYLE"
    )
    assert [item["quantity"] for item in company["rows"] if item["style"] == "OTHER_STYLE"] == [
        0 if second_complete else None
    ] * len(extra)
    assert company["automatic"] == second_complete
    custom = rules()
    custom["company_source"] = company
    layout = build_update(before, config(), custom, forecast=True)
    assert (
        any(field["platform"] == "company" for field in layout["report"]["target_fields"])
        == second_complete
    )

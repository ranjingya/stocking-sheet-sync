from copy import deepcopy
from datetime import date

import pytest

from stocking_sheet_sync.domain.models import CopyState, FillState
from stocking_sheet_sync.domain.products import inspect_sheet
from stocking_sheet_sync.services.history import HistoryFiller, remap_report
from stocking_sheet_sync.services.layout import build_update
from stocking_sheet_sync.services.sales import inspect_sales
from tests.test_layout_apply import completed, config, incoming, rules


class Reader:
    def __init__(self, before, issues=()):
        self.rows = inspect_sheet(before, config())["rows"]
        self.issues = list(issues)
        self.dates = []

    def catalog(self, source, skus):
        return [{k: row[k] for k in ("sku", "style", "name", "spec")} for row in self.rows]

    def sales(self, source, skus, as_of):
        self.dates.append(as_of)
        return {
            "platform": source["id"],
            "issues": self.issues,
            "rows": [{"sku": sku, "quantity": 10, "status": "matched"} for sku in skus],
        }


def setup_fill(tmp_path, monkeypatch, issues=()):
    before = incoming()
    layout_update = build_update(before, config(), rules())
    prepared = completed(before, layout_update)
    after = deepcopy(prepared)
    for cols in inspect_sheet(after, config())["columns"].values():
        for row in (4, 6):
            after["cells"][f"{cols['sales']}{row}"] = {"value": 10}
    reader = Reader(before, issues)
    filler = HistoryFiller(object(), reader_factory=lambda: reader)
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    reads = iter([before, prepared, after])
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.read_sheet", lambda *a, **k: next(reads)
    )
    writes = []
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.apply_update",
        lambda *a, **k: writes.append("layout") or {},
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.write_sales_ranges",
        lambda *a, **k: writes.append("sales") or {},
    )
    copy = CopyState(
        "rec",
        "source",
        "模板",
        "source-url",
        "record-url",
        "copied",
        target_token="test-token",
        target_url="target-url",
        copied_at="2026-09-12T12:00:00+08:00",
    )
    claim = FillState(
        "rec", "source", "test-token", "2026-09-12", "attempt", report_path=str(tmp_path)
    )
    return filler, copy, claim, reader, writes


def test_complete_pipeline_preserves_demand_and_uses_one_warehouse_read(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    assert filler(copy, claim)["status"] == "completed"
    assert writes == ["layout", "sales"]
    assert reader.dates == [date(2026, 9, 12)] * len(config()["platforms"])
    assert (tmp_path / "layout-verification.json").exists()
    assert (tmp_path / "sales-verification.json").exists()


def test_missing_history_blocks_layout_and_sales_before_any_mutation(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(
        tmp_path, monkeypatch, ["incomplete_daily_coverage"]
    )
    result = filler(copy, claim)
    assert result["status"] == "retryable"
    assert "incomplete_daily_coverage" in result["reason"]
    assert writes == []


def test_missing_catalog_requires_review_without_mutation(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    monkeypatch.setattr(reader, "catalog", lambda *a: [])
    assert filler(copy, claim)["status"] == "needs_review"
    assert writes == []


def test_uncertain_structural_write_requires_review_without_sales_write(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)

    def fail(*a, **k):
        writes.append("layout")
        raise RuntimeError("写入响应未知")

    monkeypatch.setattr("stocking_sheet_sync.services.history.apply_update", fail)
    assert filler(copy, claim)["status"] == "needs_review"
    assert writes == ["layout"]


def test_failed_readback_does_not_retry_sales(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.verify_sales_update",
        lambda *a: (_ for _ in ()).throw(ValueError("原公式变化")),
    )
    assert filler(copy, claim)["status"] == "needs_review"
    assert writes == ["layout", "sales"]


def test_report_remapping_rejects_changed_product():
    before = incoming()
    report = inspect_sales(Reader(before), before, config(), date(2026, 9, 12))
    prepared = completed(before, build_update(before, config(), rules()))
    assert all(e["target_cell"] for e in remap_report(report, prepared, config())["entries"])
    prepared["cells"]["B4"]["value"] = "另一个商品"
    with pytest.raises(ValueError, match="身份发生变化"):
        remap_report(report, prepared, config())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_select_sheet_by_headers_requires_unique_candidate(count):
    class Client:
        def _request(self, method, path, **kwargs):
            if path.endswith("sheets/query"):
                return {
                    "sheets": [
                        {
                            "sheet_id": str(i),
                            "title": "任意名称",
                            "resource_type": "sheet",
                            "grid_properties": {"column_count": 6, "row_count": 10},
                        }
                        for i in range(count)
                    ]
                }
            return {
                "valueRanges": [
                    {
                        "range": kwargs["params"]["ranges"],
                        "values": [["商品编码", "款式编码", "商品名称", "颜色规格", "市场部"]],
                    }
                ]
            }

    filler = HistoryFiller(Client())
    if count == 1:
        assert filler.find_sheet("token", config()) == "0"
    else:
        with pytest.raises(ValueError, match="唯一"):
            filler.find_sheet("token", config())


def test_legacy_pipeline_only_fills_recent_sales(tmp_path, monkeypatch):
    from tests.test_layout_apply import legacy_sheet

    before = legacy_sheet()
    after = deepcopy(before)
    layout = inspect_sheet(before, config())
    for col in layout["columns"].values():
        for row in layout["rows"]:
            after["cells"][f"{col['sales']}{row['row']}"] = {"value": 10}
    reader = Reader(before)
    filler = HistoryFiller(object(), reader_factory=lambda: reader)
    monkeypatch.setattr(filler, "find_sheet", lambda *a: "test")
    reads = iter([before, after])
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.read_sheet", lambda *a, **k: next(reads)
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.apply_update",
        lambda *a, **k: pytest.fail("已有完整结构不需要写表头"),
    )
    writes = []
    monkeypatch.setattr(
        "stocking_sheet_sync.services.history.write_sales_ranges",
        lambda *a, **k: writes.extend(a[2]) or {},
    )
    copy = CopyState(
        "rec",
        "source",
        "老品",
        "url",
        "record",
        "copied",
        target_token="test-token",
        target_url="url",
    )
    claim = FillState(
        "rec", "source", "test-token", "2026-09-12", "attempt", report_path=str(tmp_path)
    )
    assert filler(copy, claim)["status"] == "completed"
    assert len(writes) > 0
    assert reader.dates == [date(2026, 9, 12)] * 5
    assert (tmp_path / "sales-verification.json").exists()


def test_new_history_switch_is_independent_of_legacy_history(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    filler.new_history = True
    filler.legacy_history = False
    assert filler(copy, claim)["status"] == "completed"
    assert writes == ["layout", "sales"]


def test_new_history_disabled_does_not_query_or_write(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    filler.new_history = False
    result = filler(copy, claim)
    assert result["status"] == "completed"
    assert result["history_status"] == "disabled"
    assert not reader.dates and not writes


def test_forecast_routes_new_styles_to_history_only(tmp_path, monkeypatch):
    from stocking_sheet_sync.services.fill import ForecastFiller

    history, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    filler = ForecastFiller(
        object(), history=False, new_history=True, reader_factory=lambda: reader
    )
    monkeypatch.setattr(filler, "find_sheet", lambda *args: "test")
    monkeypatch.setattr(
        "stocking_sheet_sync.services.fill.read_sheet", lambda *args, **kwargs: incoming()
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.services.fill.inspect_forecast",
        lambda *args, **kwargs: pytest.fail("新品不可查询去年销量或预测"),
    )
    result = filler(copy, claim)
    assert result["status"] == "completed"
    assert result["forecast_status"] == "skipped"
    assert writes == ["layout", "sales"]


def test_new_forecast_enabled_preserves_history_and_reports_unsupported(tmp_path, monkeypatch):
    from stocking_sheet_sync.services.fill import ForecastFiller

    _, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    filler = ForecastFiller(
        object(),
        history=False,
        new_history=True,
        new_forecast=True,
        legacy_forecast=False,
        reader_factory=lambda: reader,
    )
    monkeypatch.setattr(filler, "find_sheet", lambda *args: "test")
    monkeypatch.setattr(
        "stocking_sheet_sync.services.fill.read_sheet", lambda *args, **kwargs: incoming()
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.services.fill.inspect_forecast",
        lambda *args, **kwargs: pytest.fail("新品不可套用老款公式"),
    )
    result = filler(copy, claim)
    assert result["status"] == "completed"
    assert result["forecast_status"] == "unsupported"
    assert writes == ["layout", "sales"]

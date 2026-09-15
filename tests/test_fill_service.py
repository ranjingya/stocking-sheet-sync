from copy import deepcopy
from datetime import date

import pytest

from stocking_sheet_sync.fill_service import HistoryFiller, remap_report
from stocking_sheet_sync.layout_apply import build_update
from stocking_sheet_sync.models import CopyState, FillState
from stocking_sheet_sync.sales_inspect import inspect_sales
from stocking_sheet_sync.sheet_matching import inspect_sheet
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
    monkeypatch.setattr("stocking_sheet_sync.fill_service.read_sheet", lambda *a, **k: next(reads))
    writes = []
    monkeypatch.setattr(
        "stocking_sheet_sync.fill_service.apply_update",
        lambda *a, **k: writes.append("layout") or {},
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.fill_service.write_sales_ranges",
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

    monkeypatch.setattr("stocking_sheet_sync.fill_service.apply_update", fail)
    assert filler(copy, claim)["status"] == "needs_review"
    assert writes == ["layout"]


def test_failed_readback_does_not_retry_sales(tmp_path, monkeypatch):
    filler, copy, claim, reader, writes = setup_fill(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "stocking_sheet_sync.fill_service.verify_sales_update",
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

import json
from copy import deepcopy
from datetime import date
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.domain.sheets.values import build_sales_update, run, verify_sales_update
from stocking_sheet_sync.services.sales import inspect_sales
from tests.test_sales_matching import CONFIG, Reader, config, snapshot


class CompleteReader(Reader):
    def sales(self, source, skus, as_of):
        return {
            "platform": source["id"],
            "issues": [],
            "rows": [
                {"sku": sku, "quantity": i, "status": "matched"} for i, sku in enumerate(skus)
            ],
        }


def ready():
    before = snapshot()
    before.update(
        spreadsheet_token="test-token",
        revision=1,
        layout={"revision": 1, "row_heights": [], "column_widths": []},
    )
    before["cells"]["F4"] = {"value": 90, "cell_styles": {"font_size": 14}}
    before["cells"]["C7"] = {"formula": "=SUM(E4:E6)", "value": 0}
    report = inspect_sales(CompleteReader(), before, config(), date(2026, 9, 12))
    return before, report


def applied(before, update):
    after = deepcopy(before)
    after["revision"] += 1
    after["layout"]["revision"] += 1
    for e in update["entries"]:
        if e["status"] == "write":
            c = after["cells"][e["target_cell"]]
            c["value"] = e["quantity"]
    after["cells"]["C7"]["value"] = 1
    return after


def test_writes_only_empty_sales_cells_and_preserves_gaps_demands_and_totals():
    before, report = ready()
    update = build_sales_update(before, report, config())
    assert update["summary"]["expected_cells"] == 10
    assert update["summary"]["write"] == 10
    ranges = [op["range"] for op in update["operations"]]
    assert "test!E4:E4" in ranges and "test!E6:E6" in ranges
    assert len(ranges) == 10
    assert all("5" not in r and "7" not in r for r in ranges)
    assert all(type(v) is int for op in update["operations"] for row in op["values"] for v in row)
    after = applied(before, update)
    result = verify_sales_update(before, after, update)
    assert result["checked_sales_cells"] == 10
    assert set(result["platform_totals"].values()) == {1}
    assert after["cells"]["F4"] == before["cells"]["F4"]
    assert len(result["recalculated_formulas"]) == 1


def test_second_run_is_noop_including_numeric_zero():
    before, report = ready()
    update = build_sales_update(before, report, config())
    after = applied(before, update)
    repeated = inspect_sales(CompleteReader(), after, config(), date(2026, 9, 12))
    again = build_sales_update(after, repeated, config())
    assert again["status"] == "unchanged"
    assert again["operations"] == []
    assert again["summary"]["unchanged"] == 10


@pytest.mark.parametrize(
    "content", [{"value": 99}, {"value": "0"}, {"value": False}, {"value": 0, "formula": "=0"}]
)
def test_existing_different_value_text_bool_or_formula_blocks_entire_write(content):
    before, _ = ready()
    before["cells"]["E4"] = content
    report = inspect_sales(CompleteReader(), before, config(), date(2026, 9, 12))
    update = build_sales_update(before, report, config())
    assert update["status"] == "needs_review"
    assert update["operations"] == []


def test_source_gap_and_unmatched_catalog_cannot_become_zero():
    before, report = ready()
    for reason in ("incomplete_daily_coverage", "catalog_sku_missing", "duplicate_source_sku"):
        damaged = deepcopy(report)
        damaged["entries"][0]["issues"] = [reason]
        update = build_sales_update(before, damaged, config())
        assert update["status"] == "needs_review"
        assert update["operations"] == []


def test_incomplete_or_wrong_coordinate_candidates_are_rejected():
    before, report = ready()
    for edit in ("missing", "wrong_target", "duplicate"):
        damaged = deepcopy(report)
        if edit == "missing":
            damaged["entries"].pop()
        elif edit == "wrong_target":
            damaged["entries"][0]["target_cell"] = "F4"
        else:
            damaged["entries"][0] = damaged["entries"][1]
        with pytest.raises(ValueError):
            build_sales_update(before, damaged, config())


@pytest.mark.parametrize(
    "cell,content",
    [
        ("E4", {"value": "0", "cell_styles": {"number_format": "0"}}),
        ("F4", {"value": 0}),
        ("C7", {"formula": "=SUM(E4:E7)", "value": 1}),
        ("C7", {"formula": "=SUM(E4:E6)", "value": "#REF!"}),
        ("E5", {"value": 0}),
    ],
)
def test_full_readback_catches_wrong_types_unrelated_changes_and_formula_errors(cell, content):
    before, report = ready()
    update = build_sales_update(before, report, config())
    after = applied(before, update)
    after["cells"][cell] = content
    with pytest.raises(ValueError):
        verify_sales_update(before, after, update)


def test_cli_blocks_version_changes_and_calls_no_writer_when_unchanged(monkeypatch, tmp_path):
    before, report = ready()
    update = build_sales_update(before, report, config())
    after = applied(before, update)
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.read_sheet", lambda *a, **k: after
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.WarehouseSettings.load", lambda *a: None
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.SalesReader", lambda *a: CompleteReader()
    )
    writer = Mock(side_effect=AssertionError("不应调用写入工具"))
    monkeypatch.setattr("stocking_sheet_sync.domain.sheets.values.write_sales_ranges", writer)
    argv = [
        "--spreadsheet-token",
        "test-token",
        "--sheet-id",
        "test",
        "--as-of",
        "2026-09-12",
        "--source-config",
        str(CONFIG),
        "--output",
        str(tmp_path),
        "--apply",
        "--expected-revision",
    ]
    assert run([*argv, "1"]) == 1
    assert run([*argv, "2"]) == 0
    assert json.loads((tmp_path / "result.json").read_text())["status"] == "unchanged"
    writer.assert_not_called()


def test_cli_rechecks_revision_after_warehouse_read_before_write(monkeypatch, tmp_path):
    before, _ = ready()
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.read_sheet", lambda *a, **k: before
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.WarehouseSettings.load", lambda *a: None
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.SalesReader", lambda *a: CompleteReader()
    )
    monkeypatch.setattr("stocking_sheet_sync.domain.sheets.values.current_revision", lambda *a: 2)
    writer = Mock(return_value={"ok": True})
    monkeypatch.setattr("stocking_sheet_sync.domain.sheets.values.write_sales_ranges", writer)
    assert (
        run(
            [
                "--spreadsheet-token",
                "test-token",
                "--sheet-id",
                "test",
                "--as-of",
                "2026-09-12",
                "--source-config",
                str(CONFIG),
                "--output",
                str(tmp_path),
                "--apply",
                "--expected-revision",
                "1",
            ]
        )
        == 1
    )
    writer.assert_not_called()


def test_cli_source_gap_blocks_even_when_target_is_already_zero(monkeypatch, tmp_path):
    before, report = ready()
    after = applied(before, build_sales_update(before, report, config()))

    class Missing(CompleteReader):
        def sales(self, source, skus, as_of):
            return {**super().sales(source, skus, as_of), "issues": ["incomplete_daily_coverage"]}

    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.read_sheet", lambda *a, **k: after
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.WarehouseSettings.load", lambda *a: None
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.domain.sheets.values.SalesReader", lambda *a: Missing()
    )
    writer = Mock(side_effect=AssertionError("来源异常时不应提交"))
    monkeypatch.setattr("stocking_sheet_sync.domain.sheets.values.write_sales_ranges", writer)
    assert (
        run(
            [
                "--spreadsheet-token",
                "test-token",
                "--sheet-id",
                "test",
                "--as-of",
                "2026-09-12",
                "--source-config",
                str(CONFIG),
                "--output",
                str(tmp_path),
                "--apply",
                "--expected-revision",
                "2",
            ]
        )
        == 2
    )
    writer.assert_not_called()


def test_sales_totals_follow_existing_platform_total_ranges_and_keep_formulas():
    before, report = ready()
    for columns in report["layout"]["columns"].values():
        col = columns["demand"]
        before["cells"][f"{col}7"] = {"formula": f"=SUM({col}4:{col}6)", "value": 0}
    before["cells"]["E7"] = {"formula": "=sum($E$4:$E$6)", "value": 0}
    update = build_sales_update(before, report, config())
    assert len(update["total_entries"]) == 5
    assert update["summary"]["total_formulas_to_write"] == 4
    assert update["total_entries"][0]["formula"] == "=sum($E$4:$E$6)"
    assert all(op["range"] != "test!E7:E7" for op in update["operations"])
    after = applied(before, update)
    for total in update["total_entries"]:
        after["cells"][total["target_cell"]] = {"formula": total["formula"], "value": 1}
    result = verify_sales_update(before, after, update)
    assert result["checked_total_formulas"] == 5
    for col in ("F", "H", "J", "L", "N"):
        assert after["cells"][f"{col}7"] == before["cells"][f"{col}7"]
    repeated = inspect_sales(CompleteReader(), after, config(), date(2026, 9, 12))
    assert build_sales_update(after, repeated, config())["operations"] == []
    after["cells"]["G7"]["value"] = "#REF!"
    with pytest.raises(ValueError, match="平台合计"):
        verify_sales_update(before, after, update)


def test_numeric_total_is_not_overwritten_and_partial_subtotals_are_not_copied():
    before, report = ready()
    before["cells"]["F7"] = {"formula": "=SUM(F4:F6)", "value": 90}
    before["cells"]["E7"] = {"value": 99}
    plan = build_sales_update(before, report, config())
    assert plan["status"] == "needs_review" and not plan["operations"]
    before["cells"]["F7"]["formula"] = "=SUM(F4:F4)"
    plan = build_sales_update(before, report, config())
    assert plan["total_entries"] == []


def test_height_change_does_not_block_but_width_change_does():
    before, report = ready()
    before["layout"]["sheet_format"] = '<sheetFormatPr baseColWidth="8" defaultRowHeight="0" />'
    update = build_sales_update(before, report, config())
    after = applied(before, update)
    after["layout"]["sheet_format"] = '<sheetFormatPr baseColWidth="8" defaultRowHeight="16" />'
    after["layout"]["row_heights"] = [16]
    assert verify_sales_update(before, after, update)["verified"]
    after["layout"]["sheet_format"] = '<sheetFormatPr baseColWidth="9" defaultRowHeight="16" />'
    with pytest.raises(ValueError, match="sheet_format"):
        verify_sales_update(before, after, update)

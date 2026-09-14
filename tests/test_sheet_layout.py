from copy import deepcopy
from pathlib import Path

import pytest

from stocking_sheet_sync.layout_preview import run
from stocking_sheet_sync.sheet_layout import load_layout_config, plan_market_layout
from stocking_sheet_sync.sheet_matching import column_name
from tests.test_sales_matching import config

ROOT = Path(__file__).resolve().parents[1]


def rules():
    return load_layout_config(ROOT / "config/sheet-layout.toml", config())


def sheet(headers=None, style="KQ26123"):
    settings = rules()
    if headers is None:
        headers = [
            settings["platforms"][p][m]
            for p in settings["layout"]["platform_order"]
            for m in ("sales", "demand")
        ]
    count = 6 + len(headers)
    cells = {f"{column_name(c)}{r}": {} for c in range(1, count + 1) for r in range(1, 8)}
    for c, v in zip("ABCD", ["款式编码", "商品编码", "商品名称", "颜色规格"], strict=True):
        cells[f"{c}3"] = {"value": v}
    cells["E2"] = {"value": "其他部门"}
    cells["E3"] = {"value": "其他需求"}
    cells["E4"] = {"formula": "=10", "value": 10}
    cells["F2"] = {"value": "市场部"}
    for c, label in enumerate(headers, 6):
        cells[f"{column_name(c)}3"] = {"value": label}
    cells[f"{column_name(count)}2"] = {"value": "渠道部"}
    for r in (4, 6):
        for c, v in zip("ABCD", [style, f"000{r}", "商品", "规格"], strict=True):
            cells[f"{c}{r}"] = {"value": v}
    cells["C7"] = {"value": "合计"}
    return {
        "title": "测试表",
        "sheet_id": "test",
        "revision": 3,
        "column_count": count,
        "row_count": 7,
        "cells": cells,
        "merges": [f"F2:{column_name(count - 1)}2"],
    }


def preview(data):
    return plan_market_layout(data, config(), rules())


def test_standard_new_fields_and_no_mutation():
    data = sheet()
    data["cells"]["F4"] = {"value": 0}
    data["cells"]["G4"] = {"formula": "=100", "value": 100}
    before = deepcopy(data)
    result = preview(data)
    assert result["status"] == "ready"
    assert len(result["target_fields"]) == 10
    assert [r["row"] for r in result["style_rows"]] == [4, 6]
    assert result["style_rows"][0]["sku"] == "0004"
    assert result["target_fields"][0]["filled_product_cells"] == 1
    assert result["target_fields"][1]["filled_product_cells"] == 1
    assert data == before


def test_missing_sales_columns_get_insertions_in_final_order():
    settings = rules()
    result = preview(
        sheet([settings["platforms"][p]["demand"] for p in settings["layout"]["platform_order"]])
    )
    assert result["status"] == "changes_proposed"
    inserts = [o for o in result["operations"] if o["action"] == "insert_market_column"]
    assert [o["position"] for o in inserts] == ["F", "H", "J", "L", "N"]
    assert [f["source_column"] for f in result["target_fields"] if f["metric"] == "demand"] == list(
        "FGHIJ"
    )
    assert all(
        o.get("cell", "").endswith("3")
        for o in result["operations"]
        if o["action"] == "set_market_field_header"
    )


def test_orphan_sales_header_expands_group_without_claiming_other_department():
    data = sheet()
    data["cells"]["F2"] = {}
    data["cells"]["G2"] = {"value": "市场部"}
    data["merges"] = ["G2:O2"]
    data["cells"]["E3"] = {"value": "唯品近30天"}
    result = preview(data)
    assert result["market_range"] == "F:O"
    assert result["operations"] == [
        {
            "action": "set_market_group_header",
            "before_range": "G2:O2",
            "after_range": "F2:O2",
            "header": "市场部",
        }
    ]


def test_other_department_merged_span_blocks_orphan_recovery():
    data = sheet()
    data["cells"]["F2"] = {}
    data["cells"]["G2"] = {"value": "市场部"}
    data["merges"] = ["E2:F2", "G2:O2"]
    result = preview(data)
    assert result["market_range"] == "G:O"
    assert result["target_fields"][0]["source_column"] is None


def test_aliases_are_renamed_and_orders_are_mapped_without_data_rewrite():
    data = sheet()
    data["cells"]["F3"]["value"] = "唯品会近30天"
    data["cells"]["L3"]["value"] = "天猫超市近30天"
    data["cells"]["N3"]["value"] = "京东pop近30天"
    data["cells"]["G3"], data["cells"]["I3"] = data["cells"]["I3"], data["cells"]["G3"]
    before = deepcopy(data)
    result = preview(data)
    assert result["status"] == "changes_proposed"
    assert result["target_fields"][1]["source_column"] == "I"
    assert any(o["action"] == "move_market_column" for o in result["operations"])
    assert any(o.get("after") == "唯品近 30 天" for o in result["operations"])
    assert data == before


@pytest.mark.parametrize("label", ["拼多多近7天", "市场部其它", "不明字段"])
def test_special_or_combined_fields_block_automatic_operations(label):
    data = sheet()
    data["cells"]["J3"]["value"] = label
    data["cells"]["J4"] = {"value": 25}
    result = preview(data)
    assert result["status"] == "needs_review"
    assert result["operations"] == []
    assert result["issues"][0]["filled_product_cells"] == 1


def legacy_sheet():
    settings = rules()
    headers = []
    for p in settings["layout"]["platform_order"]:
        platform = settings["platforms"][p]
        for m in platform.get("legacy_metrics", settings["layout"]["legacy_metrics"]):
            headers.append(
                platform[m].format(period="25.9-26.3月" if p == "jd_self" else "25.9-10月")
            )
    return sheet(headers, style="KQ25073")


def test_legacy_preserves_platform_specific_periods_and_net_shipments():
    result = preview(legacy_sheet())
    assert result["status"] == "ready"
    assert len(result["target_fields"]) == 16
    history = {
        f["platform"]: f["period_label"]
        for f in result["target_fields"]
        if f["metric"] == "history"
    }
    assert history["jd_self"] == "25.9-26.3月"
    assert history["vip"] == "25.9-10月"
    assert sum(f["metric"] == "history_net" for f in result["target_fields"]) == 1


def test_missing_legacy_period_requires_explicit_configuration():
    result = preview(sheet(style="KQ23001"))
    assert result["status"] == "needs_review"
    assert any(i["reason"] == "history_period_required" for i in result["issues"])
    settings = rules()
    settings["history_periods"] = {p: "25.9-10月" for p in settings["platforms"]}
    result = plan_market_layout(sheet(style="KQ23001"), config(), settings)
    assert result["status"] == "changes_proposed"
    assert len(result["target_fields"]) == 16


def test_conflicting_history_periods_block_plan():
    data = legacy_sheet()
    for cell in data["cells"].values():
        if cell.get("value") == "拼多多25.9-10月实发-实退":
            cell["value"] = "拼多多25.9-12月实发-实退"
    result = preview(data)
    assert any(i["reason"] == "history_period_conflict" for i in result["issues"])
    assert result["operations"] == []


@pytest.mark.parametrize("style", ["KQ25001", "KQ27001", "未知"])
def test_mixed_or_unknown_styles_are_not_assigned_one_template(style):
    data = sheet()
    data["cells"]["A6"]["value"] = style
    assert preview(data)["status"] == "needs_review"


def test_duplicate_headers_and_skus_are_blocked():
    data = sheet()
    data["cells"]["H3"] = deepcopy(data["cells"]["F3"])
    data["cells"]["B6"] = deepcopy(data["cells"]["B4"])
    result = preview(data)
    assert {i["reason"] for i in result["issues"]} >= {"duplicate_field", "product_row_invalid"}
    assert result["operations"] == []


def test_offline_cli_needs_no_warehouse_and_emits_reviewable_files(tmp_path, monkeypatch):
    import json

    path = tmp_path / "input.json"
    path.write_text(json.dumps(sheet()), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "stocking_sheet_sync.layout_preview.read_sheet",
        lambda *args: pytest.fail("不应读取线上表格"),
    )
    output = tmp_path / "preview"
    assert (
        run(
            [
                "--snapshot",
                str(path),
                "--source-config",
                str(ROOT / "config/sales-sources.toml"),
                "--layout-config",
                str(ROOT / "config/sheet-layout.toml"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads((output / "layout-preview.json").read_text())
    assert report["preview_only"] is True
    assert report["status"] == "ready"
    assert (output / "layout-preview.md").is_file()
    assert json.loads((output / "sheet-snapshot.json").read_text()) == sheet()

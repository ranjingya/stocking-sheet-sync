from copy import deepcopy

import pytest

from stocking_sheet_sync.layout_apply import build_update, run, verify_update
from stocking_sheet_sync.sheet_matching import column_name
from tests.test_sheet_layout import ROOT, config, rules, sheet


def incoming():
    data = sheet(["唯品会", "京东自营", "拼多多", "天猫超市", "京东POP"])
    data["spreadsheet_token"] = "test-token"
    data["layout"] = {
        "column_widths": [{"cols": "A:K", "width": 105}],
        "row_heights": [{"rows": "1:7", "height": 27}],
    }
    data["cells"]["F4"] = {"value": 100}
    return data


def completed(before, update):
    after = deepcopy(before)
    after["column_count"] += 5
    after["cells"] = {
        f"{column_name(c)}{r}": {}
        for c in range(1, after["column_count"] + 1)
        for r in range(1, after["row_count"] + 1)
    }
    for old, new in update["column_mapping"].items():
        for row in range(1, before["row_count"] + 1):
            after["cells"][f"{new}{row}"] = deepcopy(before["cells"][f"{old}{row}"])
    after["cells"]["G2"] = {}
    after["cells"]["F2"] = {"value": "市场部"}
    for col, name in zip(
        ["F", "H", "J", "L", "N"],
        ["唯品近30天", "自营近30天", "拼多多近30天", "猫超近30天", "京东POP近30天"],
        strict=True,
    ):
        after["cells"][f"{col}3"] = {"value": name}
    after["merges"] = ["F2:O2"]
    return after


def test_build_only_inserts_sales_and_copies_formats():
    before = incoming()
    unchanged = deepcopy(before)
    update = build_update(before, config(), rules())
    assert before == unchanged
    assert update["column_mapping"]["F"] == "G"
    assert update["column_mapping"]["K"] == "P"
    assert update["inserted_columns"] == ["F", "H", "J", "L", "N"]
    assert all(
        o["input"]["inherit_style"] == "before"
        for o in update["operations"]
        if o["shortcut"] == "+dim-insert"
    )
    assert all(
        o["input"]["paste_type"] == "formats"
        for o in update["operations"]
        if o["shortcut"] == "+range-copy"
    )
    assert not any(o["shortcut"] in {"+dim-delete", "+range-move"} for o in update["operations"])
    assert not any(
        "formula" in c
        for o in update["operations"]
        for row in o["input"].get("cells", [])
        for c in row
    )


def test_repeated_execution_is_empty_and_keeps_existing_quantity():
    before = incoming()
    update = build_update(before, config(), rules())
    after = completed(before, update)
    assert verify_update(before, after, update, config(), rules())["verified"]
    assert after["cells"]["G4"]["value"] == 100
    assert build_update(after, config(), rules())["operations"] == []


@pytest.mark.parametrize(
    "cell,content",
    [
        ("E4", {"value": 20}),
        ("G4", {"value": 0}),
        ("F4", {"value": 100}),
        ("G5", {"cell_styles": {"font_size": 20}}),
    ],
)
def test_verifier_rejects_content_loss_and_style_changes(cell, content):
    before = incoming()
    update = build_update(before, config(), rules())
    after = completed(before, update)
    after["cells"][cell] = content
    with pytest.raises(ValueError):
        verify_update(before, after, update, config(), rules())


def test_special_period_and_missing_demand_prevent_write():
    data = incoming()
    data["cells"]["F3"] = {"value": "唯品近30天"}
    with pytest.raises(ValueError, match="需求列"):
        build_update(data, config(), rules())
    data["cells"]["F3"] = {"value": "唯品近7天"}
    with pytest.raises(ValueError, match="结构明确"):
        build_update(data, config(), rules())


def test_expected_revision_rejected_before_batch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "stocking_sheet_sync.layout_apply.read_sheet", lambda *args, **kwargs: incoming()
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.layout_apply._batch",
        lambda *args, **kwargs: pytest.fail("版本不符时不应提交"),
    )
    assert (
        run(
            [
                "--spreadsheet-token",
                "test-token",
                "--sheet-id",
                "test",
                "--source-config",
                str(ROOT / "config/sales-sources.toml"),
                "--layout-config",
                str(ROOT / "config/sheet-layout.toml"),
                "--apply",
                "--expected-revision",
                "2",
                "--output",
                str(tmp_path),
            ]
        )
        == 1
    )

from copy import deepcopy

import pytest

from stocking_sheet_sync.domain.products import column_name
from stocking_sheet_sync.entrypoints.layout_apply import run
from stocking_sheet_sync.services.layout import build_update, verify_update
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


def test_build_only_inserts_sales_and_inherits_demand_styles():
    before = incoming()
    unchanged = deepcopy(before)
    update = build_update(before, config(), rules())
    assert before == unchanged
    assert update["column_mapping"]["F"] == "G"
    assert update["column_mapping"]["K"] == "P"
    assert update["inserted_columns"] == ["F", "H", "J", "L", "N"]
    inserts = [o for o in update["operations"] if o["endpoint"] == "insert_dimension_range"]
    assert [o["body"]["dimension"]["startIndex"] for o in inserts] == [5, 7, 9, 11, 13]
    assert all(o["body"]["inheritStyle"] == "AFTER" for o in inserts)
    assert all(o["method"] in {"POST", "PUT"} for o in update["operations"])
    assert set(o["endpoint"] for o in update["operations"]) == {
        "insert_dimension_range",
        "dimension_range",
        "unmerge_cells",
        "merge_cells",
        "values_batch_update",
    }


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
        "stocking_sheet_sync.entrypoints.layout_apply.read_sheet",
        lambda *args, **kwargs: incoming(),
    )
    monkeypatch.setattr(
        "stocking_sheet_sync.entrypoints.layout_apply.apply_update",
        lambda *args, **kwargs: pytest.fail("版本不符时不应提交"),
    )
    assert (
        run(
            [
                "--spreadsheet-token",
                "test-token",
                "--sheet-id",
                "test",
                "--config",
                str(ROOT / "config/config.example.toml"),
                "--config",
                str(ROOT / "config/config.example.toml"),
                "--apply",
                "--expected-revision",
                "2",
                "--output",
                str(tmp_path),
            ]
        )
        == 1
    )


def test_new_sales_column_inherits_total_formula_without_demand_values():
    before = incoming()
    before["cells"]["F7"] = {"formula": "=SUM(F4:F6)", "value": 100}
    update = build_update(before, config(), rules())
    assert update["total_formulas"] == {"F7": "=SUM(F4:F6)"}
    assert before["cells"]["F7"]["value"] == 100
    assert any(
        op["body"].get("valueRanges")
        == [{"range": "test!F7:F7", "values": [[{"type": "formula", "text": "=SUM(F4:F6)"}]]}]
        for op in update["operations"]
    )
    after = completed(before, update)
    after["cells"]["G7"]["formula"] = "=SUM(G4:G6)"
    after["cells"]["F7"] = {"formula": "=SUM(F4:F6)", "value": 0}
    assert verify_update(before, after, update, config(), rules())["verified"]


def test_native_steps_stop_on_uncertain_insert_without_retry():
    from stocking_sheet_sync.services.layout import apply_update

    calls = []

    class Client:
        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return {"revision": 1}
            raise RuntimeError("插列响应未知")

    before = incoming()
    update = build_update(before, config(), rules())
    journal = []
    with pytest.raises(RuntimeError, match="未知"):
        apply_update(
            "token",
            "test",
            update,
            1,
            client=Client(),
            on_progress=lambda j: journal.append(deepcopy(j)),
        )
    assert len([c for c in calls if c[0] != "GET"]) == 1
    assert calls[-1][2]["retry"] is False
    assert journal[-1]["steps"][-1]["status"] == "sending"


def test_native_layout_rejects_revision_change_before_mutation():
    from stocking_sheet_sync.services.layout import apply_update

    class Client:
        def _request(self, method, path, **kwargs):
            assert method == "GET"
            return {"revision": 9}

    with pytest.raises(ValueError, match="版本"):
        apply_update(
            "token", "test", build_update(incoming(), config(), rules()), 1, client=Client()
        )


def legacy_sheet(*, sales=True):
    settings = rules()
    headers = []
    for pid in settings["layout"]["platform_order"]:
        titles = settings["platforms"][pid]
        headers.append(titles["history"].format(period="25.9-10月"))
        if pid == "pdd":
            headers.append(titles["history_net"].format(period="25.9-12月"))
        if sales:
            headers.append(titles["sales"])
        headers.append(titles["demand"])
    data = sheet(headers, style="KQ25123")
    data["spreadsheet_token"] = "test-token"
    data["layout"] = {}
    for c in range(6, data["column_count"]):
        label = data["cells"][f"{column_name(c)}3"]["value"]
        if "近30天" not in label:
            data["cells"][f"{column_name(c)}4"] = {"value": 123}
            data["cells"][f"{column_name(c)}6"] = {"formula": "=50+50", "value": 100}
    return data


def test_legacy_existing_periods_and_values_remain_untouched():
    before = legacy_sheet()
    original = deepcopy(before)
    update = build_update(before, config(), rules())
    assert update["report"]["category"] == "legacy"
    assert not update["operations"]
    assert before == original
    assert verify_update(before, deepcopy(before), update, config(), rules())["verified"]


def test_legacy_missing_sales_only_inserts_five_recent_columns():
    before = legacy_sheet(sales=False)
    update = build_update(before, config(), rules())
    assert len(update["inserted_columns"]) == 5
    assert all(
        f["metric"] == "sales" for f in update["report"]["target_fields"] if not f["source_column"]
    )
    for field in update["report"]["target_fields"]:
        if field["metric"] in {"history", "history_net"}:
            assert field["header"] == before["cells"][field["source_column"] + "3"]["value"]


def test_legacy_blank_template_needs_no_business_history_dates():
    before = incoming()
    for row in (4, 6):
        before["cells"][f"A{row}"]["value"] = "KQ25123"
    update = build_update(before, config(), rules())
    assert len(update["inserted_columns"]) == 5
    assert {f["metric"] for f in update["report"]["target_fields"]} == {"sales", "demand"}
    after = completed(before, update)
    assert verify_update(before, after, update, config(), rules())["verified"]


def test_layout_verifier_preserves_rich_text_segments():
    before = incoming()
    before["cells"]["E4"] = {
        "value": "备注",
        "rich_text": [{"type": "text", "text": "备注", "segmentStyle": {"bold": True}}],
    }
    update = build_update(before, config(), rules())
    after = completed(before, update)
    assert verify_update(before, after, update, config(), rules())["verified"]
    after["cells"]["E4"]["rich_text"][0]["segmentStyle"]["bold"] = False
    with pytest.raises(ValueError, match="rich_text"):
        verify_update(before, after, update, config(), rules())


def test_layout_height_change_is_allowed():
    before = incoming()
    before["layout"]["sheet_format"] = '<sheetFormatPr baseColWidth="8" defaultRowHeight="0" />'
    update = build_update(before, config(), rules())
    after = completed(before, update)
    after["layout"]["sheet_format"] = '<sheetFormatPr baseColWidth="8" defaultRowHeight="16" />'
    after["layout"]["row_heights"] = [{"rows": "1:7", "height": 16}]
    assert verify_update(before, after, update, config(), rules())["verified"]

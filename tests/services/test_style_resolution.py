"""内部款号识别、身份校验及原表保护。"""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.domain.products import inspect_sheet, match_catalog
from stocking_sheet_sync.domain.sheets.layout import plan_market_layout
from stocking_sheet_sync.services.sales import resolve_sheet_styles
from tests.services.test_layout_apply import config, incoming, rules


@pytest.mark.parametrize("raw", [None, "", 6942649422352, "错误款号"])
def test_fallback_uses_unique_sku_without_changing_cells(raw):
    snapshot = incoming()
    cfg = config()
    for i, row in enumerate((4, 6)):
        snapshot["cells"][f"B{row}"]["value"] = f"694264942235{i}"
    catalog = deepcopy(inspect_sheet(snapshot, cfg)["rows"])
    for row in (4, 6):
        snapshot["cells"][f"A{row}"]["value"] = raw
    before = deepcopy(snapshot)
    reader = Mock()
    reader.catalog.return_value = catalog
    resolve_sheet_styles(reader, snapshot, cfg)
    layout = inspect_sheet(snapshot, cfg)
    match_catalog(layout, catalog)
    assert all(row["match_status"] == "matched" for row in layout["rows"])
    assert plan_market_layout(snapshot, cfg, rules())["category"] == "new"
    assert snapshot == before
    snapshot["cells"]["A4"]["value"] = "KQ25001"
    changed = inspect_sheet(snapshot, cfg)
    match_catalog(changed, catalog)
    assert "style_mismatch" in changed["rows"][0]["issues"]


@pytest.mark.parametrize("failure", ["missing", "ambiguous", "name", "spec", "kq_conflict"])
def test_unsafe_identity_is_never_resolved(failure):
    snapshot = incoming()
    cfg = config()
    snapshot["cells"]["B4"]["value"] = "6942649422352"
    catalog = deepcopy(inspect_sheet(snapshot, cfg)["rows"])
    snapshot["cells"]["A4"]["value"] = "KQ25001" if failure == "kq_conflict" else "6942649422352"
    if failure == "missing":
        catalog = catalog[1:]
    elif failure == "ambiguous":
        catalog.append(deepcopy(catalog[0]))
    elif failure in {"name", "spec"}:
        catalog[0][failure] = "冲突内容"
    reader = Mock()
    reader.catalog.return_value = catalog
    resolve_sheet_styles(reader, snapshot, cfg)
    assert not cfg.get("resolved_styles")
    layout = inspect_sheet(snapshot, cfg)
    match_catalog(layout, catalog)
    assert layout["rows"][0]["match_status"] == "needs_review"
    if failure == "kq_conflict":
        reader.catalog.assert_not_called()

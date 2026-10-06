from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from stocking_sheet_sync.infrastructure.company import CompanyReader, field_texts
from stocking_sheet_sync.settings import business_view, load_forecast_sources


def provider():
    cfg = business_view(Path("config/config.toml"), "company")
    reader, client = Mock(), Mock()
    client._request.side_effect = [
        {
            "items": [
                {
                    "field_name": "销售出库分组-全公司",
                    "field_id": "lookup",
                    "type": 19,
                    "property": {
                        "filter_info": {"target_table": "maintenance"},
                        "target_field": "group",
                    },
                }
            ]
        },
        {
            "items": [
                {
                    "field_name": "分类",
                    "field_id": "group",
                    "type": 3,
                    "property": {
                        "options": [
                            {"id": "opt-company", "name": "公司"},
                            {"id": "opt-excluded", "name": "不计入"},
                        ]
                    },
                }
            ]
        },
        {
            "items": [
                {
                    "fields": {
                        "聚水潭店铺编码": [{"text": "1"}],
                        "销售出库分组-全公司": {"type": 3, "value": ["opt-company"]},
                    }
                }
            ],
            "has_more": True,
            "page_token": "next",
        },
        {
            "items": [
                {
                    "fields": {
                        "聚水潭店铺编码": [{"text": "2"}],
                        "销售出库分组-全公司": {"value": ["不计入"]},
                    }
                }
            ],
            "has_more": False,
        },
    ]
    return CompanyReader(
        reader,
        cfg,
        load_forecast_sources(Path("config/config.toml"))["daily"]["jd_self"],
        client=client,
    )


def test_shop_paging_projection_and_per_run_cache():
    p = provider()
    assert p.shop_groups() == {"1": "公司", "2": "不计入"}
    assert p.shop_groups() == {"1": "公司", "2": "不计入"}
    assert p.client._request.call_count == 4
    assert p.client._request.call_args.kwargs["params"]["page_token"] == "next"
    assert field_texts({"value": [{"text": "公司"}]}) == ["公司"]


def test_shop_paging_missing_cursor_is_not_complete():
    p = provider()
    p.group_options = lambda: {}
    p.client._request.side_effect = [{"items": [], "has_more": True}]
    with pytest.raises(RuntimeError, match="分页不完整"):
        p.shop_groups()


@pytest.mark.parametrize(
    "kind", ["complete", "unknown_shop", "invalid", "jd_gap", "date_gap", "conflict"]
)
def test_company_combination_preserves_zero_excludes_pull_goods_and_isolates_sku(kind):
    p = provider()
    p.reader._read.side_effect = [
        [] if kind == "date_gap" else [{"day": "2025-09-28"}],
        [
            {
                "sku": "A",
                "shop": "unknown" if kind == "unknown_shop" else "1",
                "quantity": 12,
                "invalid_count": int(kind == "invalid"),
                "conflicts": int(kind == "conflict"),
            },
            {"sku": "A", "shop": "2", "quantity": 9999, "invalid_count": 0, "conflicts": 0},
        ],
    ]
    p.reader.daily_window.return_value = {
        "issues": ["daily_source_needs_review"] if kind == "jd_gap" else [],
        "rows": [
            {
                "sku": "A",
                "quantity": None if kind == "jd_gap" else 3,
                "status": "needs_review" if kind == "jd_gap" else "matched",
            },
            {"sku": "B", "quantity": 0, "status": "matched"},
        ],
    }
    rows = p.window(["A", "B"], date(2025, 9, 28), date(2025, 9, 29))
    assert rows[0]["quantity"] == (
        15 if kind == "complete" else 3 if kind == "unknown_shop" else None
    )
    assert rows[1]["quantity"] == (None if kind == "date_gap" else 0)
    sql, params = p.reader._read.call_args.args
    assert "outstock_order_detail_id" in sql and "COUNT(DISTINCT `shop_id`)" in sql
    assert "`dept`" not in sql and "`shop_id` IN" not in sql
    assert "公司" in params and "销售出库" in params and params[-2:] == ("A", "B")


def test_duplicate_shop_classification_is_unknown():
    p = provider()
    p.group_options = lambda: {}
    p.client._request.side_effect = [
        {
            "items": [
                {"fields": {"聚水潭店铺编码": "1", "销售出库分组-全公司": "公司"}},
                {"fields": {"聚水潭店铺编码": "1", "销售出库分组-全公司": "不计入"}},
            ],
            "has_more": False,
        }
    ]
    assert p.shop_groups() == {"1": ""}

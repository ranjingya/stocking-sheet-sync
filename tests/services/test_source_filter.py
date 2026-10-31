"""专供源表筛选及复制前阻断验证。"""

from unittest.mock import Mock

import pytest

from stocking_sheet_sync.services.source_filter import exclusive_source_reason
from tests.services.test_sync_service import make_service


def client_for_headers(headers):
    """构造每张工作表的表头读取结果。"""
    client = Mock()
    sheets = [
        {"sheet_id": str(i), "grid_properties": {"column_count": 20, "row_count": 100}}
        for i in range(len(headers))
    ]
    client._request.side_effect = [
        {"sheets": sheets},
        *[
            {"valueRanges": [{"range": f"{i}!A1:T3", "values": [row]}]}
            for i, row in enumerate(headers)
        ],
    ]
    return client


@pytest.mark.parametrize("keyword", ["抖音专供", "天猫专供", "淘宝专供"])
@pytest.mark.parametrize(
    "pattern", ["{platform}专供款", "{platform}27年速干t专供款备料需求", "专供款-{platform}"]
)
@pytest.mark.parametrize("force", [False, True])
def test_exclusive_source_skips_before_copy_and_notification(tmp_path, keyword, pattern, force):
    service, client, _, _ = make_service(tmp_path)
    record = client.get_base_record("rec_test")
    record.fields["下单表格"]["text"] = pattern.format(platform=keyword.removesuffix("专供"))
    client.get_base_record = Mock(return_value=record)
    client._request = client_for_headers([["抖音部"]])._request
    result = service.run_record("rec_test", force=force, request_id="batch" if force else "")
    assert result.result == "skipped"
    assert keyword in result.reason
    assert client.copy_count == 0 and client.sent_cards == []
    assert service.store.get_state("rec_test", client.source_token) is None


@pytest.mark.parametrize("title", ["普通需求", "直播间专供", "抖音下单", "专供款备料需求"])
def test_other_titles_do_not_request_headers(title):
    client = Mock()
    assert exclusive_source_reason(client, "token", title, "config/config.toml") == ""
    client._request.assert_not_called()


def test_market_header_in_any_sheet_preserves_source():
    client = client_for_headers([["抖音部"], ["市 场 部"]])
    assert (
        exclusive_source_reason(
            client, "token", "抖音27年速干t专供款备料需求", "config/config.toml"
        )
        == ""
    )


@pytest.mark.parametrize(
    "payload",
    [
        RuntimeError("读取失败"),
        {"sheets": []},
        {"sheets": [{"sheet_id": "0", "grid_properties": {"column_count": 20, "row_count": 100}}]},
    ],
)
def test_unavailable_headers_never_classified_exclusive(payload):
    client = Mock()
    client._request.side_effect = [payload, {"valueRanges": []}]
    with pytest.raises((RuntimeError, ValueError)):
        exclusive_source_reason(client, "token", "淘宝专供", "config/config.toml")

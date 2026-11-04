"""专供源表筛选及复制前阻断验证。"""

from unittest.mock import Mock

import pytest

from stocking_sheet_sync.services.source_filter import exclusive_source_reason
from tests.services.test_sync_service import make_service


@pytest.mark.parametrize("keyword", ["抖音专供", "天猫专供", "淘宝专供"])
@pytest.mark.parametrize(
    "pattern",
    [
        "{platform}专供款",
        "{platform}27年速干t专供款备料需求",
        "专供款-{platform}",
        "{platform} 27年冬款 专供需求",
    ],
)
@pytest.mark.parametrize("force", [False, True])
def test_exclusive_source_skips_before_copy_and_notification(tmp_path, keyword, pattern, force):
    service, client, _, _ = make_service(tmp_path)
    record = client.get_base_record("rec_test")
    record.fields["下单表格"]["text"] = pattern.format(platform=keyword.removesuffix("专供"))
    client.get_base_record = Mock(return_value=record)
    client._request = Mock(side_effect=AssertionError("标题过滤不应读取工作表或市场部表头"))
    result = service.run_record("rec_test", force=force, request_id="batch" if force else "")
    assert result.result == "skipped"
    assert keyword in result.reason
    assert client.copy_count == 0 and client.sent_cards == []
    client._request.assert_not_called()
    assert service.store.get_state("rec_test", client.source_token) is None


@pytest.mark.parametrize("title", ["普通需求", "直播间专供", "抖音下单", "专供款备料需求"])
def test_other_titles_do_not_request_headers(title):
    assert exclusive_source_reason(title, "config/config.toml") == ""


@pytest.mark.parametrize("keywords", ["天猫专供", [None], [""], ["   "]])
def test_invalid_keywords_are_rejected(monkeypatch, keywords):
    monkeypatch.setattr(
        "stocking_sheet_sync.services.source_filter.business_view",
        lambda *a: {"exclusive_title_keywords": keywords},
    )
    with pytest.raises(ValueError):
        exclusive_source_reason("天猫专供款", "config/config.toml")

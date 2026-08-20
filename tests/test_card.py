from stocking_sheet_sync.card import build_sync_card


def test_build_success_card_contains_record_and_target_links() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        status="success",
    )

    assert card["header"]["template"] == "green"
    content = card["body"]["elements"][0]["content"]
    assert "备货测试表" in content
    assert "record-token" in content
    assert "target-token" in content


def test_build_failure_card_contains_reason() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        status="failure",
        reason="没有访问权限",
    )

    assert card["header"]["template"] == "red"
    assert "没有访问权限" in card["body"]["elements"][0]["content"]

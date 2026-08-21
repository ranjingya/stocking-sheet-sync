from stocking_sheet_sync.card import build_sync_card


def test_build_success_card_contains_record_and_target_links() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        status="success",
        sync_type="initial",
        target_folder_token="folder-token",
    )

    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "产品下单同步 · 同步成功"
    assert card["config"]["summary"]["content"] == "产品下单同步 · 同步成功"
    content = card["body"]["elements"][0]["content"]
    assert "备货测试表" in content
    assert "record-token" in content
    assert "target-token" in content
    actions = card["body"]["elements"][1]["columns"]
    assert actions[1]["elements"][0]["behaviors"][0]["default_url"] == (
        "https://example.feishu.cn/drive/folder/folder-token"
    )


def test_build_update_card_uses_yellow_theme() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        status="success",
        sync_type="update",
        target_folder_token="folder-token",
    )

    assert card["header"]["template"] == "yellow"
    assert card["header"]["title"]["content"] == "产品下单同步 · 更新成功"
    assert "最新副本" in card["body"]["elements"][0]["content"]


def test_build_failure_card_contains_reason() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        status="failure",
        sync_type="update",
        target_folder_token="folder-token",
        reason="没有访问权限",
    )

    assert card["header"]["template"] == "red"
    assert "没有访问权限" in card["body"]["elements"][0]["content"]


def test_build_deleted_record_card_links_latest_copy() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        target_name="市场部-备货测试表-v2",
        target_url="https://example.feishu.cn/sheets/target-token",
        status="deleted",
        sync_type="update",
        target_folder_token="folder-token",
    )

    assert card["header"]["template"] == "red"
    assert card["header"]["title"]["content"] == "产品下单同步 · 原记录已删除"
    content = card["body"]["elements"][0]["content"]
    assert "原始记录： 备货测试表" in content
    assert "市场部-备货测试表-v2" in content
    assert "target-token" in content

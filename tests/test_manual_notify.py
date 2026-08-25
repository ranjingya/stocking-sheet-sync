from __future__ import annotations

from typing import Any

from stocking_sheet_sync.manual_notify import send_manual_notification


class FakeMessageClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def send_card(self, open_id: str, card: dict[str, Any]) -> None:
        self.sent.append((open_id, card))


def test_manual_update_notification_sends_yellow_card_to_all_recipients() -> None:
    client = FakeMessageClient()

    summary = send_manual_notification(
        client,
        ("ou_first", "ou_second", "ou_first"),
        mode="update",
        original_name="备货测试记录",
        original_url="https://example.feishu.cn/record/source-token",
        target_name="市场部-备货测试表-v2",
        target_url="https://example.feishu.cn/sheets/target-token",
        target_folder_token="folder-token",
    )

    assert summary.sent == 2
    assert summary.failed == 0
    assert [item[0] for item in client.sent] == ["ou_first", "ou_second"]
    card = client.sent[0][1]
    assert card["header"]["template"] == "yellow"
    assert card["header"]["title"]["content"] == "产品下单同步 · 更新成功"


def test_manual_copy_notification_uses_green_card() -> None:
    client = FakeMessageClient()

    summary = send_manual_notification(
        client,
        ("ou_first",),
        mode="copy",
        original_name="备货测试记录",
        original_url="https://example.feishu.cn/record/source-token",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        target_folder_token="folder-token",
    )

    assert summary.sent == 1
    assert summary.failed == 0
    assert client.sent[0][1]["header"]["template"] == "green"

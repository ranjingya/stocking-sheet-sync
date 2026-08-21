from pathlib import Path

import pytest

from stocking_sheet_sync.config import load_config

CONFIG_TEXT = """
[feishu]
api_base_url = "https://open.feishu.cn"

[source]
app_token = "base-token"
table_id = "table-id"
view_id = "view-id"
link_field_name = "下单表格"
required_fields = { "状态" = "需求收集" }
monitor_required_fields = { "状态" = "需求收集" }
monitor_days = 3

[target]
folder_token = "folder-token"
copy_name_prefix = "市场部-"

[notifications]
open_ids = ["ou_first", "ou_second", "ou_first"]

[redis]
key_prefix = "ss-test"

[web]
public_base_url = "https://stock-sync.example.com"

[runtime]
poll_interval_minutes = 30
change_check_interval_minutes = 1
change_quiet_minutes = 10
request_timeout_seconds = 20
max_retries = 4
log_level = "WARNING"
"""


def test_load_config_separates_credentials_and_business_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")

    config = load_config(
        env={
            "FEISHU_DATA_APP_ID": "cli_data",
            "FEISHU_DATA_APP_SECRET": "data-secret",
            "FEISHU_MESSAGE_APP_ID": "cli_message",
            "FEISHU_MESSAGE_APP_SECRET": "message-secret",
            "REDIS_URL": "redis://redis.example:6379/2",
            "WEBHOOK_SECRET": "webhook-secret",
        },
        config_path=config_path,
    )

    assert config.feishu_data_app_id == "cli_data"
    assert config.feishu_data_app_secret == "data-secret"
    assert config.feishu_message_app_id == "cli_message"
    assert config.feishu_message_app_secret == "message-secret"
    assert config.base_app_token == "base-token"
    assert config.required_fields == {"状态": "需求收集"}
    assert config.monitor_required_fields == {"状态": "需求收集"}
    assert config.monitor_days == 3
    assert config.notify_open_ids == ("ou_first", "ou_second")
    assert config.redis_url == "redis://redis.example:6379/2"
    assert config.redis_key_prefix == "ss-test"
    assert config.poll_interval_minutes == 30
    assert config.change_check_interval_minutes == 1
    assert config.change_quiet_minutes == 10
    assert config.log_level == "WARNING"
    assert config.public_base_url == "https://stock-sync.example.com"
    assert config.webhook_secret == "webhook-secret"


def test_load_config_rejects_invalid_open_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TEXT.replace('"ou_first", "ou_second", "ou_first"', '"anycross_user_1"'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="格式错误"):
        load_config(
            env={
                "FEISHU_DATA_APP_ID": "cli_data",
                "FEISHU_DATA_APP_SECRET": "data-secret",
                "FEISHU_MESSAGE_APP_ID": "cli_message",
                "FEISHU_MESSAGE_APP_SECRET": "message-secret",
                "WEBHOOK_SECRET": "webhook-secret",
            },
            config_path=config_path,
        )


def test_load_config_supports_legacy_message_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")

    config = load_config(
        env={
            "FEISHU_DATA_APP_ID": "cli_data",
            "FEISHU_DATA_APP_SECRET": "data-secret",
            "FEISHU_APP_ID": "cli_message",
            "FEISHU_APP_SECRET": "message-secret",
            "WEBHOOK_SECRET": "webhook-secret",
        },
        config_path=config_path,
    )

    assert config.feishu_message_app_id == "cli_message"
    assert config.feishu_message_app_secret == "message-secret"

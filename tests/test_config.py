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

[target]
folder_token = "folder-token"
copy_name_prefix = "市场部-"

[notifications]
open_ids = ["ou_first", "ou_second", "ou_first"]

[redis]
key_prefix = "stocking-sheet-sync-test"

[runtime]
poll_interval_minutes = 5
request_timeout_seconds = 20
max_retries = 4
error_notify_cooldown_minutes = 120
log_level = "WARNING"
"""


def test_load_config_separates_credentials_and_business_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")

    config = load_config(
        env={
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "secret",
            "REDIS_URL": "redis://redis.example:6379/2",
        },
        config_path=config_path,
    )

    assert config.feishu_app_id == "cli_test"
    assert config.base_app_token == "base-token"
    assert config.required_fields == {"状态": "需求收集"}
    assert config.notify_open_ids == ("ou_first", "ou_second")
    assert config.redis_url == "redis://redis.example:6379/2"
    assert config.redis_key_prefix == "stocking-sheet-sync-test"
    assert config.log_level == "WARNING"


def test_load_config_rejects_invalid_open_id(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        CONFIG_TEXT.replace('"ou_first", "ou_second", "ou_first"', '"anycross_user_1"'),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="格式错误"):
        load_config(
            env={"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "secret"},
            config_path=config_path,
        )

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class AppConfig:
    feishu_data_app_id: str
    feishu_data_app_secret: str
    feishu_message_app_id: str
    feishu_message_app_secret: str
    feishu_api_base_url: str
    base_app_token: str
    base_table_id: str
    base_view_id: str | None
    link_field_name: str
    required_fields: dict[str, Any]
    target_folder_token: str
    copy_name_prefix: str
    notify_open_ids: tuple[str, ...]
    poll_interval_minutes: float
    change_check_interval_minutes: float
    change_quiet_minutes: float
    redis_url: str
    redis_key_prefix: str
    request_timeout_seconds: float
    max_retries: int
    log_level: str
    public_base_url: str
    webhook_secret: str


def load_config(
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> AppConfig:
    """
    功能说明：从 .env 读取应用凭证，从 TOML 文件读取业务和运行配置。

    参数：
        env：可选环境变量映射；未传入时加载 .env 并读取当前进程环境。
        config_path：可选 TOML 配置路径；优先级高于 CONFIG_PATH 环境变量。

    返回值：
        完成类型转换和校验的 AppConfig。
    """
    if env is None:
        load_dotenv()
        environment: Mapping[str, str] = os.environ
    else:
        environment = env

    selected_path = Path(
        config_path or environment.get("CONFIG_PATH", "./config.toml")
    ).expanduser()
    selected_path = selected_path.resolve()
    document = _read_toml(selected_path)

    feishu = _table(document, "feishu")
    source = _table(document, "source")
    target = _table(document, "target")
    notifications = _table(document, "notifications")
    redis = _table(document, "redis")
    runtime = _table(document, "runtime")
    web = _table(document, "web")

    required_fields = source.get("required_fields", {})
    if not isinstance(required_fields, dict):
        raise ValueError("source.required_fields 必须是 TOML 对象")

    return AppConfig(
        feishu_data_app_id=_require_env(environment, "FEISHU_DATA_APP_ID"),
        feishu_data_app_secret=_require_env(environment, "FEISHU_DATA_APP_SECRET"),
        feishu_message_app_id=_require_env_with_fallback(
            environment,
            "FEISHU_MESSAGE_APP_ID",
            "FEISHU_APP_ID",
        ),
        feishu_message_app_secret=_require_env_with_fallback(
            environment,
            "FEISHU_MESSAGE_APP_SECRET",
            "FEISHU_APP_SECRET",
        ),
        feishu_api_base_url=_text(feishu.get("api_base_url", "https://open.feishu.cn")).rstrip("/"),
        base_app_token=_required_text(source, "app_token", "source.app_token"),
        base_table_id=_required_text(source, "table_id", "source.table_id"),
        base_view_id=_optional_text(source.get("view_id")),
        link_field_name=_text(source.get("link_field_name", "下单表格")) or "下单表格",
        required_fields=required_fields,
        target_folder_token=_required_text(target, "folder_token", "target.folder_token"),
        copy_name_prefix=_text(target.get("copy_name_prefix", "市场部-")),
        notify_open_ids=_parse_open_ids(notifications.get("open_ids", [])),
        poll_interval_minutes=_positive_float(
            runtime.get("poll_interval_minutes", 30), "runtime.poll_interval_minutes"
        ),
        change_check_interval_minutes=_positive_float(
            runtime.get("change_check_interval_minutes", 1),
            "runtime.change_check_interval_minutes",
        ),
        change_quiet_minutes=_positive_float(
            runtime.get("change_quiet_minutes", 10), "runtime.change_quiet_minutes"
        ),
        redis_url=(environment.get("REDIS_URL", "").strip() or "redis://localhost:6379/0"),
        redis_key_prefix=_parse_key_prefix(redis.get("key_prefix")),
        request_timeout_seconds=_positive_float(
            runtime.get("request_timeout_seconds", 15), "runtime.request_timeout_seconds"
        ),
        max_retries=_positive_int(runtime.get("max_retries", 3), "runtime.max_retries"),
        log_level=_parse_log_level(runtime.get("log_level", "INFO")),
        public_base_url=_parse_public_base_url(web.get("public_base_url")),
        webhook_secret=environment.get("WEBHOOK_SECRET", "").strip(),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"配置文件不存在：{path}")
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"TOML 配置格式错误：{path}：{error}") from error
    return document


def _table(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"配置文件缺少 [{name}] 区块")
    return value


def _require_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"缺少必填环境变量：{name}")
    return value


def _require_env_with_fallback(
    env: Mapping[str, str],
    name: str,
    fallback_name: str,
) -> str:
    value = env.get(name, "").strip() or env.get(fallback_name, "").strip()
    if not value:
        raise ValueError(f"缺少必填环境变量：{name}")
    return value


def _required_text(table: dict[str, Any], key: str, field_name: str) -> str:
    value = _optional_text(table.get(key))
    if not value:
        raise ValueError(f"配置项不能为空：{field_name}")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = _text(value).strip()
    return text or None


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"配置值必须是字符串：{value!r}")
    return value


def _positive_float(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} 必须是正数：{value!r}")
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须是正数：{value!r}")
    return parsed


def _positive_int(value: object, field_name: str) -> int:
    parsed = _positive_float(value, field_name)
    if not parsed.is_integer():
        raise ValueError(f"{field_name} 必须是正整数：{value!r}")
    return int(parsed)


def _parse_open_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("notifications.open_ids 必须是字符串数组")
    open_ids = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    invalid = [item for item in open_ids if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", item)]
    if invalid:
        raise ValueError(f"notifications.open_ids 中存在格式错误的 open_id：{', '.join(invalid)}")
    return open_ids


def _parse_log_level(value: object) -> str:
    level = _text(value).strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"runtime.log_level 不受支持：{value!r}")
    return level


def _parse_key_prefix(value: object) -> str:
    prefix = _optional_text(value)
    if not prefix:
        raise ValueError("配置项不能为空：redis.key_prefix")
    prefix = prefix.rstrip(":")
    if not prefix:
        raise ValueError("redis.key_prefix 不能只包含冒号")
    return prefix


def _parse_public_base_url(value: object) -> str:
    url = _required_text(
        {"public_base_url": value},
        "public_base_url",
        "web.public_base_url",
    ).rstrip("/")
    if not re.fullmatch(r"https://[^\s/]+(?:/[^\s]*)?", url):
        raise ValueError("web.public_base_url 必须是有效的 HTTPS 地址")
    return url

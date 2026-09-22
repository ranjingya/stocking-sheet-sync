from __future__ import annotations

import logging
import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

from stocking_sheet_sync.domain.products import (
    normalize_text,
)

LOG = logging.getLogger(__name__)
HISTORY_METRICS = {"history", "history_net"}


@dataclass(frozen=True, slots=True)
class AppConfig:
    feishu_data_app_id: str
    feishu_data_app_secret: str
    feishu_message_app_id: str
    feishu_message_app_secret: str
    feishu_api_base_url: str
    base_app_token: str
    base_table_id: str
    link_field_name: str
    required_fields: dict[str, Any]
    target_folder_token: str
    copy_name_prefix: str
    notify_open_ids: tuple[str, ...]
    failure_notify_open_ids: tuple[str, ...]
    lock_ttl_seconds: int
    redis_url: str
    redis_key_prefix: str
    request_timeout_seconds: float
    max_retries: int
    log_level: str
    public_base_url: str
    webhook_secret: str
    queue_max_attempts: int = 3
    queue_retry_delay_seconds: int = 10
    queue_result_ttl_seconds: int = 604800
    config_path: str = "config/config.toml"
    fill_new_history_enabled: bool = False
    fill_new_forecast_enabled: bool = False
    temp_max_files: int = 100
    temp_max_bytes: int = 1073741824
    fill_history_enabled: bool = False
    fill_forecast_enabled: bool = False
    fill_report_dir: str = "artifacts/fill"
    backup_folder_token: str = ""


def load_config(
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> AppConfig:
    """
    功能说明：从 .env 读取应用凭证，从 TOML 文件读取业务和运行配置。

    参数：
        env：可选环境变量映射；未传入时加载 .env 并读取当前进程环境。
        config_path：可选 TOML 配置路径；默认读取当前工作目录下的 config/config.toml。

    返回值：
        完成类型转换和校验的 AppConfig。
    """
    if env is None:
        load_dotenv()
        environment: Mapping[str, str] = os.environ
    else:
        environment = env

    selected_path = Path(config_path or "config/config.toml").expanduser()
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
        config_path=str(selected_path),
        queue_max_attempts=_positive_int(
            runtime.get("queue_max_attempts", 3), "runtime.queue_max_attempts"
        ),
        queue_retry_delay_seconds=_positive_int(
            runtime.get("queue_retry_delay_seconds", 10), "runtime.queue_retry_delay_seconds"
        ),
        queue_result_ttl_seconds=_positive_int(
            runtime.get("queue_result_ttl_seconds", 604800), "runtime.queue_result_ttl_seconds"
        ),
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
        link_field_name=_text(source.get("link_field_name", "下单表格")) or "下单表格",
        required_fields=required_fields,
        target_folder_token=_required_text(target, "folder_token", "target.folder_token"),
        copy_name_prefix=_text(target.get("copy_name_prefix", "市场部-")),
        backup_folder_token=_required_text(
            target, "backup_folder_token", "target.backup_folder_token"
        ),
        notify_open_ids=_parse_open_ids(
            notifications.get("open_ids", []),
            "notifications.open_ids",
        ),
        failure_notify_open_ids=_parse_open_ids(
            notifications.get("failure_open_ids", []),
            "notifications.failure_open_ids",
        ),
        lock_ttl_seconds=_positive_int(
            runtime.get("lock_ttl_seconds", 300), "runtime.lock_ttl_seconds"
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
        fill_new_forecast_enabled=_env_bool(environment, "FILL_NEW_FORECAST_ENABLED"),
        fill_new_history_enabled=_env_bool(environment, "FILL_NEW_HISTORY_ENABLED"),
        temp_max_files=_positive_int(runtime.get("temp_max_files", 100), "runtime.temp_max_files"),
        temp_max_bytes=_positive_int(
            runtime.get("temp_max_bytes", 1073741824), "runtime.temp_max_bytes"
        ),
        fill_history_enabled=_env_bool(
            environment,
            "FILL_OLD_HISTORY_ENABLED"
            if "FILL_OLD_HISTORY_ENABLED" in environment
            else "FILL_HISTORY_ENABLED",
        ),
        fill_forecast_enabled=_env_bool(
            environment,
            "FILL_OLD_FORECAST_ENABLED"
            if "FILL_OLD_FORECAST_ENABLED" in environment
            else "FILL_FORECAST_ENABLED",
        ),
        fill_report_dir=_text(runtime.get("fill_report_dir", "artifacts/fill")),
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
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{field_name} 必须是正数：{value!r}")
    return parsed


def _positive_int(value: object, field_name: str) -> int:
    parsed = _positive_float(value, field_name)
    if not parsed.is_integer():
        raise ValueError(f"{field_name} 必须是正整数：{value!r}")
    return int(parsed)


def _parse_open_ids(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} 必须是字符串数组")
    open_ids = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    invalid = [item for item in open_ids if not re.fullmatch(r"ou_[A-Za-z0-9_-]+", item)]
    if invalid:
        raise ValueError(f"{field_name} 中存在格式错误的 open_id：{', '.join(invalid)}")
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


def _env_bool(env: Mapping[str, str], name: str) -> bool:
    """解析环境变量 env 中的开关 name；缺省关闭，非法值阻止启动。"""
    value = env.get(name, "false").strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false、1/0、yes/no 或 on/off")


def business_view(path: Path, section: str) -> dict:
    """
    功能说明：读取独立业务规则并组装指定模块的字段视图。

    参数：
        path：运行配置路径或规则文件路径；规则固定读取同目录 rules.toml。
        section：sales、layout、forecast 或 sources。
    返回值：对应业务模块的独立配置字典。
    """
    rules_path = path.parent / "rules.toml"
    with rules_path.open("rb") as stream:
        data = tomllib.load(stream)
    if section == "forecast":
        return data["forecast"]
    platforms = data["platforms"]
    if section == "layout":
        return {**data["sheet"], "platforms": {pid: p["layout"] for pid, p in platforms.items()}}
    if section == "sales":
        return {
            "catalog": data["catalog"],
            "matching": data["matching"],
            "platforms": [
                {
                    "id": pid,
                    **{
                        k: v
                        for k, v in p.items()
                        if k
                        not in {
                            "layout",
                            "daily_fields",
                            "rolling_fields",
                            "business_date_offset_days",
                        }
                    },
                }
                for pid, p in platforms.items()
            ],
        }
    if section == "sources":
        return {
            "catalog": data["catalog"],
            "daily": {
                pid: {
                    "id": pid,
                    "kind": "daily",
                    "table": p["table"],
                    "fields": p["daily_fields"],
                    "filters": p["filters"],
                    "rolling_fields": p["rolling_fields"],
                    "business_date_offset_days": p["business_date_offset_days"],
                }
                for pid, p in platforms.items()
                if "daily_fields" in p
            },
        }
    raise ValueError(f"未知配置视图：{section}")


def identifier(value: str) -> str:
    """校验并引用 SQL 标识符 value，返回安全的反引号标识符。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("数仓表名或字段名格式无效")
    return f"`{value}`"


@dataclass(frozen=True)
class WarehouseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = dataclass_field(repr=False)
    connect_timeout: int = 10
    read_timeout: int = 30

    @classmethod
    def load(cls, env_file: Path | None = None, env: Mapping[str, str] | None = None):
        """
        功能说明：读取本项目的 WAREHOUSE_* 数仓连接参数，环境变量优先。

        参数：
            env_file：可选凭证文件路径；默认只读当前目录的 .env，不搜索父目录。
            env：可选环境变量映射，默认使用进程环境。

        返回值：经过校验且隐藏密码展示的连接设置。
        """
        if env_file is not None and not env_file.is_file():
            raise ValueError("数仓凭证文件不存在")
        selected_file = env_file if env_file is not None else Path(".env")
        values = {
            **(dotenv_values(selected_file) if selected_file.is_file() else {}),
            **(os.environ if env is None else env),
        }
        names = ("WAREHOUSE_HOST", "WAREHOUSE_DATABASE", "WAREHOUSE_USER", "WAREHOUSE_PASSWORD")
        if any(not values.get(name) for name in names):
            raise ValueError("数仓连接配置不完整，请填写本项目的 WAREHOUSE_* 必填参数")
        port = int(values.get("WAREHOUSE_PORT", "3306"))
        connect = int(values.get("WAREHOUSE_CONNECT_TIMEOUT", "10"))
        read = int(values.get("WAREHOUSE_READ_TIMEOUT", "30"))
        if not 1 <= port <= 65535 or not 1 <= connect <= 60 or not 1 <= read <= 120:
            raise ValueError("数仓端口或超时设置无效")
        return cls(
            values["WAREHOUSE_HOST"],
            port,
            values["WAREHOUSE_DATABASE"],
            values["WAREHOUSE_USER"],
            values["WAREHOUSE_PASSWORD"],
            connect,
            read,
        )


def load_sales_config(path: Path) -> dict[str, Any]:
    """
    功能说明：加载可替换的平台、字段、筛选与表头匹配配置。

    参数：
        path：业务 TOML 配置路径。

    返回值：校验后的配置对象；无效的 SQL 标识符或重复平台会报错。
    """
    config = business_view(path, "sales")
    platforms = config["platforms"]
    if not platforms or len({p["id"] for p in platforms}) != len(platforms):
        raise ValueError("平台配置不能为空或包含重复 ID")
    matching = config["matching"]
    if not isinstance(matching["header_rows"], int) or not 1 <= matching["header_rows"] <= 20:
        raise ValueError("表头行数需要在 1 到 20 之间")
    for source in [config["catalog"], *platforms]:
        identifier(source["table"])
        for name in source["fields"].values():
            identifier(name)
        for name in source.get("unique_fields", []):
            identifier(name)
        for name, values in source.get("filters", {}).items():
            identifier(name)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) for v in values)
            ):
                raise ValueError("筛选条件必须是非空字符串数组")
    if not {"sku", "style", "name", "spec"} <= config["catalog"]["fields"].keys():
        raise ValueError("商品主数据字段配置不完整")
    for platform in platforms:
        if platform["kind"] not in {"snapshot", "detail"}:
            raise ValueError("平台来源类型只支持 snapshot 或 detail")
        required = {"sku", "date", "quantity"}
        if platform["kind"] == "snapshot":
            required.add("row_id")
        if not required <= platform["fields"].keys():
            raise ValueError("平台来源字段配置不完整")
        if platform["kind"] == "detail" and not platform.get("unique_fields"):
            raise ValueError("明细来源必须配置唯一键字段")
        if not platform.get("sales_headers") or not platform.get("demand_headers"):
            raise ValueError("平台需要配置销量及需求表头")
    return config


def load_forecast_config(path: Path = Path("config/config.toml")) -> dict:
    """读取并校验 path 指定的季节及兜底规则，返回配置字典。"""
    rules = business_view(path, "forecast")
    seasons = rules["seasons"]
    labels = []
    for name in ("summer", "winter", "all_year"):
        values = seasons[f"{name}_labels"]
        if not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("季节标签必须为非空字符串列表")
        labels.extend(values)
    if len(labels) != len(set(labels)):
        raise ValueError("季节标签不能重复或跨季节配置")
    for name in ("summer_end", "winter_end"):
        month, day = seasons[name]
        date(2001, month, day)
    for name in ("sku_count_below", "sales_below"):
        value = rules["fallback"][name]
        if type(value) is not int or value <= 0:
            raise ValueError("兜底阈值必须为正整数")
    LOG.debug("预测规则加载完成：path=%s", path)
    return rules


def load_layout_config(path: Path, sales_config: dict) -> dict:
    """
    功能说明：读取市场部表头、款式分类及平台顺序规则，并校验配置一致性。

    参数：
        path：结构规则 TOML 文件路径。
        sales_config：提供平台 ID、表头别名和商品字段的销量配置。

    返回值：通过校验的结构规则字典。
    """
    rules = business_view(path, "layout")
    layout = rules["layout"]
    pattern = re.compile(layout["style_pattern"])
    if "year" not in pattern.groupindex:
        raise ValueError("款式分类正则必须包含 year 分组")
    if type(layout["new_year"]) is not int:
        raise ValueError("新品年份必须是整数")
    group_row = layout["group_row"]
    if type(group_row) is not int or not 1 <= group_row < sales_config["matching"]["header_rows"]:
        raise ValueError("市场部分类行必须位于字段表头之前")
    order = layout["platform_order"]
    ids = {p["id"] for p in sales_config["platforms"]}
    if len(order) != len(set(order)) or set(order) != ids or set(rules["platforms"]) != ids:
        raise ValueError("结构平台顺序必须完整覆盖销量配置中的平台，且不能重复")
    if normalize_text(layout["market_header"]) not in {
        normalize_text(v) for v in sales_config["matching"]["market_headers"]
    }:
        raise ValueError("市场部标题必须属于匹配配置中的市场部别名")
    aliases = {}
    for platform in sales_config["platforms"]:
        pid = platform["id"]
        settings = rules["platforms"][pid]
        if "forecast" in rules and not str(settings.get("future_prefix", "")).strip():
            raise ValueError(f"平台 {pid} 缺少后续周期日期表头前缀")
        for category in ("new", "legacy"):
            metrics = settings.get(f"{category}_metrics", layout[f"{category}_metrics"])
            if (
                not metrics
                or len(metrics) != len(set(metrics))
                or not {"sales", "demand"} <= set(metrics)
                or not set(metrics) <= HISTORY_METRICS | {"sales", "demand"}
                or (category == "new" and set(metrics) & HISTORY_METRICS)
            ):
                raise ValueError(f"平台 {pid} 的 {category} 字段配置无效")
            for metric in metrics:
                if not isinstance(settings.get(metric), str) or not settings[metric].strip():
                    raise ValueError(f"平台 {pid} 缺少 {metric} 标题")
                if metric in HISTORY_METRICS:
                    regex = re.compile(settings[f"{metric}_pattern"])
                    if "period" not in regex.groupindex or settings[metric].count("{period}") != 1:
                        raise ValueError("历史字段必须包含 period 分组和表头占位符")
                    if re.search(r"[{}]", settings[metric].replace("{period}", "")):
                        raise ValueError("历史表头只支持 period 占位符")
        for metric in ("sales", "demand"):
            for label in [settings[metric], *platform[f"{metric}_headers"]]:
                normalized = normalize_text(label)
                if normalized in aliases and aliases[normalized] != (pid, metric):
                    raise ValueError("不同市场部字段的别名不能重叠")
                aliases[normalized] = (pid, metric)
    if not set(rules.get("history_periods", {})) <= ids:
        raise ValueError("历史区间配置包含未知平台")
    if any(
        not isinstance(v, str) or not v.strip() for v in rules.get("history_periods", {}).values()
    ):
        raise ValueError("历史区间配置必须是非空文本")
    if "forecast" in rules:
        forecast = rules["forecast"]
        if forecast.get("metrics") != ["sales", "previous", "future", "forecast", "demand"]:
            raise ValueError("公式预估指标顺序需要包含三项历史、公式预估及人工需求")
        names = [
            forecast.get(key)
            for key in ("company_header", "previous_suffix", "future_suffix", "forecast_header")
        ]
        if any(not isinstance(v, str) or not v.strip() for v in names) or len(set(names)) != 4:
            raise ValueError("公式预估表头配置必须非空且互不重复")
        for template in [forecast["forecast_header"], *forecast.get("forecast_aliases", [])]:
            if not isinstance(template, str) or template.count("{platform}") != 1:
                raise ValueError("预估标题模板必须包含一个platform占位符")
            if re.search(r"[{}]", template.replace("{platform}", "")):
                raise ValueError("预估标题模板只支持platform占位符")
    LOG.debug("市场部结构规则加载完成：path=%s platforms=%d", path, len(order))
    return rules


def load_forecast_sources(path: Path) -> dict:
    """校验 path 中预测来源的标识符和筛选，返回配置字典。"""
    config = business_view(path, "sources")
    for source in [config["catalog"], *config.get("daily", {}).values()]:
        identifier(source["table"])
        for field in [*source["fields"].values(), *source.get("rolling_fields", {}).values()]:
            identifier(field)
        for field, values in source.get("filters", {}).items():
            identifier(field)
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) or not v for v in values)
            ):
                raise ValueError("预测来源筛选必须是非空字符串数组")
    if not {"sku", "style", "name", "spec", "labels"} <= config["catalog"]["fields"].keys():
        raise ValueError("预测商品主数据字段不完整")
    for source in config.get("daily", {}).values():
        if (
            source["kind"] != "daily"
            or not {"sku", "date", "quantity", "row_id", "rolling_30"} <= source["fields"].keys()
        ):
            raise ValueError("预测日快照字段不完整")
        for days in source.get("rolling_fields", {}):
            if not str(days).isdigit() or not 1 <= int(days) <= 366:
                raise ValueError("滚动周期必须为1至366天")
        if type(source.get("business_date_offset_days")) is not int:
            raise ValueError("日快照必须明确业务日期偏移")
    return config

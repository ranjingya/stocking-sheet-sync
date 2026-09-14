from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


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
    password: str = field(repr=False)
    connect_timeout: int = 10
    read_timeout: int = 30

    @classmethod
    def load(cls, env_file: Path | None = None, env: Mapping[str, str] | None = None):
        """
        功能说明：读取与 jd-insight 一致的 DB_* 连接参数，环境变量优先。

        参数：
            env_file：可选凭证文件路径，只读加载。
            env：可选环境变量映射，默认使用进程环境。

        返回值：经过校验且隐藏密码展示的连接设置。
        """
        if env_file is not None and not env_file.is_file():
            raise ValueError("数仓凭证文件不存在")
        values = {
            **(dotenv_values(env_file) if env_file else {}),
            **(os.environ if env is None else env),
        }
        if values.get("DB_DRIVER", "mysql+pymysql") != "mysql+pymysql":
            raise ValueError("数仓驱动需要使用 mysql+pymysql")
        names = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
        if any(not values.get(name) for name in names):
            raise ValueError("数仓连接配置不完整，需要 DB_HOST、DB_NAME、DB_USER、DB_PASSWORD")
        port = int(values.get("DB_PORT", "3306"))
        connect = int(values.get("DB_CONNECT_TIMEOUT", "10"))
        read = int(values.get("DB_READ_TIMEOUT", "30"))
        if not 1 <= port <= 65535 or not 1 <= connect <= 60 or not 1 <= read <= 120:
            raise ValueError("数仓端口或超时设置无效")
        return cls(
            values["DB_HOST"],
            port,
            values["DB_NAME"],
            values["DB_USER"],
            values["DB_PASSWORD"],
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
    with path.open("rb") as file:
        config = tomllib.load(file)
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

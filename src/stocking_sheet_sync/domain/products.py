from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any

LOG = logging.getLogger(__name__)


def column_name(index: int) -> str:
    """将从 1 开始的列号 index 转为 A1 列字母。"""
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def column_number(value: str) -> int:
    """将 A1 列字母 value 转为从 1 开始的列号。"""
    number = 0
    for letter in value:
        number = number * 26 + ord(letter) - 64
    return number


def normalize_text(value: Any) -> str:
    """将标签或规格 value 规范化为空白无关的小写文本。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def sku_text(value: Any) -> str:
    """保留文本商品编码的前导零；数值编码必须能精确表达，否则报错。"""
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?[eE][+-]?\d+", value):
            raise ValueError("科学计数法商品编码需要先恢复精确文本")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("商品编码类型无效")
    if not 0 <= value < 10**15 or int(value) != value:
        raise ValueError("数值商品编码存在精度风险")
    return str(int(value))


def cells_from_envelope(envelope: dict) -> dict[str, dict]:
    """
    功能说明：从飞书单元格读取结果提取精确坐标，并拒绝截断数据。

    参数：
        envelope：归档单元格快照的 JSON 信封。

    返回值：以 A1 坐标为键的单元格字典。
    """
    if envelope.get("ok") is not True:
        raise ValueError("飞书单元格读取未成功")
    data = envelope["data"]
    if data.get("has_more"):
        raise ValueError("单元格读取已截断，请缩小分页后重读")
    result = {}
    for item in data["ranges"]:
        if item.get("truncated"):
            raise ValueError("单元格范围读取不完整")
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", item["actual_range"])
        if not match:
            raise ValueError("单元格实际范围格式无效")
        left, top, right, bottom = match.groups()
        expected_rows = list(range(int(top), int(bottom) + 1))
        expected_cols = [
            column_name(c) for c in range(column_number(left), column_number(right) + 1)
        ]
        if item["row_indices"] != expected_rows or item["col_indices"] != expected_cols:
            raise ValueError("单元格坐标存在缺口，必须包含隐藏行列")
        if len(item["cells"]) != len(expected_rows):
            raise ValueError("单元格实际行数与坐标不符")
        for row, values in zip(item["row_indices"], item["cells"], strict=True):
            if len(values) != len(expected_cols):
                raise ValueError("单元格实际列数与坐标不符")
            for col, cell in zip(item["col_indices"], values, strict=True):
                result[f"{col}{row}"] = cell
    return result


def inspect_sheet(snapshot: dict, config: dict) -> dict:
    """
    功能说明：解析商品行和市场部各平台销量、需求列，并记录缺列或编码异常。

    参数：
        snapshot：包含 cells、merges、row_count、column_count 和 sheet_id 的完整快照。
        config：含匹配别名及平台配置的业务配置。

    返回值：商品行、平台列映射和布局异常，所有坐标均指向原工作表。
    """
    cells = snapshot["cells"]
    expected = {
        f"{column_name(c)}{r}"
        for r in range(1, snapshot["row_count"] + 1)
        for c in range(1, snapshot["column_count"] + 1)
    }
    if cells.keys() != expected:
        raise ValueError("本地快照必须包含完整工作表坐标，不能省略空行或隐藏行列")
    matching = config["matching"]
    header_rows = matching["header_rows"]
    header = {key: cell.get("value") for key, cell in cells.items()}
    for area in snapshot["merges"]:
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", area)
        if not match:
            raise ValueError("合并单元格范围无效")
        left, top, right, bottom = match.groups()
        for r in range(int(top), min(int(bottom), header_rows) + 1):
            for c in range(column_number(left), column_number(right) + 1):
                header[f"{column_name(c)}{r}"] = header.get(f"{left}{top}")

    def find_columns(aliases, market_only=False):
        wanted = {normalize_text(alias) for alias in aliases}
        market = {normalize_text(alias) for alias in matching["market_headers"]}
        found = []
        for c in range(1, snapshot["column_count"] + 1):
            col = column_name(c)
            labels = {normalize_text(header.get(f"{col}{r}")) for r in range(1, header_rows + 1)}
            if labels & wanted and (not market_only or labels & market):
                found.append(col)
        return found

    product_cols = {}
    for key in ("sku", "style", "name", "spec"):
        columns = find_columns(matching[f"{key}_headers"])
        if len(columns) != 1:
            raise ValueError(f"商品字段 {key} 需要且只能对应一列：{columns}")
        product_cols[key] = columns[0]
    issues, platform_columns = [], {}
    for platform in config["platforms"]:
        mapped = {}
        for kind in ("sales", "demand"):
            found = find_columns(platform[f"{kind}_headers"], market_only=True)
            mapped[kind] = found[0] if len(found) == 1 else None
            if len(found) != 1:
                issues.append(
                    {
                        "platform": platform["id"],
                        "kind": kind,
                        "reason": "missing_column" if not found else "ambiguous_column",
                        "candidates": found,
                    }
                )
        if mapped["sales"] and mapped["sales"] == mapped["demand"]:
            raise ValueError("销量与需求不得共用同一列")
        platform_columns[platform["id"]] = mapped
    claimed = [v[k] for v in platform_columns.values() for k in ("sales", "demand") if v[k]]
    if len(set(claimed)) != len(claimed):
        raise ValueError("不同平台的目标列发生重叠")
    rows = []
    for row_no in range(header_rows + 1, snapshot["row_count"] + 1):
        row = {
            key: cells.get(f"{col}{row_no}", {}).get("value") for key, col in product_cols.items()
        }
        if not row["sku"] and not row["style"]:
            continue
        row["row"] = row_no
        row["issues"] = []
        try:
            row["sku"] = sku_text(row["sku"])
            if not row["sku"]:
                row["issues"].append("missing_sku")
        except ValueError as error:
            row["sku"] = None
            row["issues"].append(str(error))
        rows.append(row)
    duplicates = Counter(row["sku"] for row in rows if row["sku"])
    for row in rows:
        if row["sku"] and duplicates[row["sku"]] > 1:
            row["issues"].append("duplicate_sheet_sku")
    LOG.debug(
        "表格映射完成：sheet_id=%s sku_rows=%d layout_issues=%d",
        snapshot["sheet_id"],
        len(rows),
        len(issues),
    )
    return {
        "sheet_id": snapshot["sheet_id"],
        "rows": rows,
        "columns": platform_columns,
        "product_columns": product_cols,
        "issues": issues,
        "combined_columns": find_columns(matching["combined_headers"], market_only=True),
    }


def match_catalog(layout: dict, catalog_rows: list[dict]) -> None:
    """
    功能说明：按精确 SKU 匹配主数据，并校对款式、名称及规格。

    参数：
        layout：待补充匹配状态的布局对象。
        catalog_rows：数仓商品主数据行，保留重复以识别歧义。

    返回值：无，匹配状态和异常原位写入商品行。
    """
    by_sku = defaultdict(list)
    for item in catalog_rows:
        by_sku[sku_text(item["sku"])].append(item)
    for row in layout["rows"]:
        candidates = by_sku.get(row["sku"], [])
        if not candidates:
            row["issues"].append("catalog_sku_missing")
        elif len(candidates) != 1:
            row["issues"].append("catalog_sku_ambiguous")
        else:
            row["catalog"] = candidates[0]
            for key in ("style", "name", "spec"):
                if not normalize_text(row[key]) or not normalize_text(candidates[0][key]):
                    row["issues"].append(f"{key}_missing")
                elif normalize_text(row[key]) != normalize_text(candidates[0][key]):
                    row["issues"].append(f"{key}_mismatch")
        row["match_status"] = "matched" if not row["issues"] else "needs_review"
        LOG.debug(
            "商品匹配结果：row=%d sku=%s status=%s issues=%s",
            row["row"],
            row["sku"],
            row["match_status"],
            row["issues"],
        )


def units(value: Any) -> int:
    """将非负整件数 value 转为整数；空值、非整数和无穷值均报错。"""
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("数仓件数不是有效数字") from None
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError("数仓件数必须是有限的非负整数")
    return int(parsed)

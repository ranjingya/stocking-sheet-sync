"""表格布局比较时将行高差异记录为日志。"""

import logging
from xml.etree import ElementTree

LOG = logging.getLogger(__name__)


def comparable_layout(layout: dict) -> dict:
    """返回排除行高属性的布局副本，保留隐藏状态、样式及列宽。"""
    result = dict(layout)
    result.pop("row_heights", None)
    if isinstance(result.get("row_dimensions"), dict):
        result["row_dimensions"] = {
            row: {k: v for k, v in attrs.items() if k not in {"ht", "height", "customHeight"}}
            for row, attrs in result["row_dimensions"].items()
        }
    if result.get("sheet_format"):
        element = ElementTree.fromstring(result["sheet_format"])
        for key in ("defaultRowHeight", "customHeight"):
            element.attrib.pop(key, None)
        result["sheet_format"] = ElementTree.tostring(element, encoding="unicode")
    if result != layout:
        LOG.info("布局比较排除行高属性；其他布局属性继续校验")
    return result


def log_height_changes(before: dict, after: dict) -> None:
    """记录可能涉及行高的导出属性差异，不阻断交付。"""
    for key in ("row_heights", "row_dimensions", "sheet_format"):
        if before.get(key) != after.get(key):
            LOG.info("行高相关布局属性存在差异，按其余属性继续校验：%s", key)

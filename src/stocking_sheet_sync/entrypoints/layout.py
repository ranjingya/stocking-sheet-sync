from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import httpx

from stocking_sheet_sync.domain.sheets.layout import load_layout_config, plan_market_layout
from stocking_sheet_sync.infrastructure.feishu.sheets import read_sheet
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.source_settings import load_sales_config

LOG = logging.getLogger(__name__)


def _describe_operation(operation: dict) -> str:
    """将拟议操作 operation 转成中文说明，返回包含具体位置的文本。"""
    action = operation["action"]
    if action == "insert_market_column":
        return f"在 {operation['position']} 列前插入一列，承载缺失的市场部字段。"
    if action == "move_market_column":
        return (
            f"将市场部 {operation['source']} 列移至 {operation['position']} 列位置，"
            "整列内容随字段保留。"
        )
    if action == "set_market_field_header":
        before = operation["before"] or "空白"
        return f"{operation['cell']}：{before} → {operation['after']}。"
    return f"市场部合并表头：{operation['before_range']} → {operation['after_range']}。"


def write_preview(output: Path, report: dict, snapshot: dict) -> None:
    """
    功能说明：保存结构预览和输入快照，生成便于人工查看的 Markdown。

    参数：
        output：本地输出目录。
        report：市场部字段映射、拟议操作及阻断原因。
        snapshot：用于生成预览的完整工作表快照。

    返回值：无，写入 JSON 和 Markdown 文件。
    """
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("layout-preview.json", report), ("sheet-snapshot.json", snapshot)):
        (output / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def cell(value):
        return str(value if value is not None else "待确定").replace("|", "\\|").replace("\n", " ")

    status = {
        "ready": "结构符合规则",
        "changes_proposed": "已生成调整建议",
        "needs_review": "需要人工核对",
    }[report["status"]]
    category = {"new": "新品", "legacy": "老品", "mixed": "混合款式", "unknown": "无法识别"}[
        report["category"]
    ]
    metrics = {
        "sales": "近30天销量",
        "demand": "需求数量",
        "history": "往年区间销量",
        "history_net": "往年实发－实退",
    }
    lines = [
        "# 市场部结构预览",
        "",
        f"表格：{cell(report['title'])}；工作表：{report['sheet_id']}；版本：{report['revision']}。",
        f"状态：{status}；款式类型：{category}；商品行数：{len(report['style_rows'])}。",
        "",
        "本报告只预览市场部字段，不执行表格写入。现有数量、公式和其他部门内容保持原样。",
        "",
        "| 平台 | 指标 | 目标表头 | 现有列 | 目标列 | 已填商品单元格数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for field in report["target_fields"]:
        lines.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    field["platform_name"],
                    metrics[field["metric"]],
                    field["header"],
                    field["source_column"] or "待新增",
                    field["target_column"],
                    field["filled_product_cells"],
                )
            )
            + " |"
        )
    lines += ["", "## 拟议操作", ""]
    lines += [f"- {_describe_operation(op)}" for op in report["operations"]] or ["无。"]
    lines += ["", "插入或移动操作中的位置按列出的操作顺序解释；目标列表示调整后的预计位置。"]
    lines += ["", "## 待核对事项", ""]
    lines += [f"- `{json.dumps(issue, ensure_ascii=False)}`" for issue in report["issues"]] or [
        "无。"
    ]
    lines += ["", *[f"- {warning}" for warning in report["warnings"]]]
    (output / "layout-preview.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    LOG.info("市场部结构预览已保存：output=%s status=%s", output, report["status"])


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：读取线上表格或本地快照，生成独立于数仓连接的市场部结构预览。

    参数：
        argv：命令行参数列表；默认读取进程参数。

    返回值：成功生成报告返回 0；输入或读取失败返回 1。结构待核对事项写入报告。
    """
    parser = argparse.ArgumentParser(description="生成市场部表头结构调整预览，不写入飞书")
    parser.add_argument("--source-config", type=Path, default=Path("config/sales-sources.toml"))
    parser.add_argument("--layout-config", type=Path, default=Path("config/sheet-layout.toml"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--spreadsheet-token")
    group.add_argument("--snapshot", type=Path)
    parser.add_argument("--sheet-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.spreadsheet_token and not args.sheet_id:
        parser.error("读取线上表格必须同时提供 --sheet-id")
    configure_logging("INFO")
    try:
        config = load_sales_config(args.source_config)
        rules = load_layout_config(args.layout_config, config)
        snapshot = (
            json.loads(args.snapshot.read_text(encoding="utf-8"))
            if args.snapshot
            else read_sheet(args.spreadsheet_token, args.sheet_id)
        )
        report = plan_market_layout(snapshot, config, rules)
        write_preview(args.output, report, snapshot)
        return 0
    except (
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        OSError,
        httpx.TransportError,
    ) as error:
        LOG.error("市场部结构预览未完成：%s", error)
        return 1


def main() -> None:
    raise SystemExit(run())

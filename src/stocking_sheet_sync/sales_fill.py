from __future__ import annotations

import argparse
import json
import logging
import subprocess
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from .layout_apply import _batch
from .logging_config import configure_logging
from .sales_config import WarehouseSettings, load_sales_config
from .sales_inspect import _lark_read, inspect_sales, read_sheet, write_report
from .sales_reader import SalesReader, units
from .sheet_matching import column_number, inspect_sheet

LOG = logging.getLogger(__name__)


def build_sales_update(snapshot: dict, report: dict, config: dict) -> dict:
    """
    功能说明：核对完整销量候选清单，只为商品行的空白销量单元格生成请求。

    参数：
        snapshot：本次读取的完整表格值、公式与样式快照。
        report：基于同一快照和指定日期生成的数仓检查结果。
        config：商品字段、平台来源及表头别名配置。

    返回值：写入请求、逐项状态、平台合计和统计；存在任何异常时操作清单为空。
    """
    layout = inspect_sheet(snapshot, config)
    if layout["issues"] or not layout["rows"]:
        raise ValueError("商品或平台列结构不完整，请先核对表头")
    if report["spreadsheet_token"] != snapshot["spreadsheet_token"]:
        raise ValueError("销量检查结果与目标表格不一致")
    expected = {
        (p["id"], row["row"]): (row["sku"], f"{layout['columns'][p['id']]['sales']}{row['row']}")
        for p in config["platforms"]
        for row in layout["rows"]
    }
    actual = [(e["platform"], e["row"]) for e in report["entries"]]
    if len(actual) != len(expected) or set(actual) != expected.keys():
        raise ValueError("销量候选条数或商品平台组合不完整")
    entries, pending = [], defaultdict(list)
    totals = defaultdict(int)
    for entry in report["entries"]:
        sku, target = expected[(entry["platform"], entry["row"])]
        if entry["sku"] != sku or entry["target_cell"] != target:
            raise ValueError("销量候选的商品编码或目标单元格不一致")
        cell = snapshot["cells"][target]
        problems = set(entry["issues"]) - {"target_not_empty"}
        quantity = entry["observed_quantity"]
        if quantity is None:
            problems.add("quantity_missing")
        else:
            quantity = units(quantity)
        value = cell.get("value")
        equal = type(value) in (int, float) and value == quantity
        if cell.get("formula") or (value not in (None, "") and not equal):
            problems.add("target_conflict")
        status = "needs_review" if problems else "unchanged" if equal else "write"
        item = {**entry, "quantity": quantity, "status": status, "issues": sorted(problems)}
        entries.append(item)
        if status != "needs_review":
            totals[entry["platform"]] += quantity
        if status == "write":
            pending[target.rstrip("0123456789")].append(item)
        LOG.info(
            "销量填充核对：platform=%s sku=%s cell=%s status=%s issues=%s",
            entry["platform"],
            sku,
            target,
            status,
            sorted(problems),
        )
    counts = Counter(e["status"] for e in entries)
    operations = []
    if not counts["needs_review"]:
        for col in sorted(pending, key=column_number):
            # 按连续商品行分块，空行、合计行及已有值不会进入写入范围。
            blocks = []
            for entry in sorted(pending[col], key=lambda e: e["row"]):
                if (
                    not blocks
                    or entry["row"] != blocks[-1][-1]["row"] + 1
                    or len(blocks[-1]) >= 100
                ):
                    blocks.append([])
                blocks[-1].append(entry)
            for block in blocks:
                operations.append(
                    {
                        "shortcut": "+cells-set",
                        "input": {
                            "sheet_id": snapshot["sheet_id"],
                            "range": f"{col}{block[0]['row']}:{col}{block[-1]['row']}",
                            "allow_overwrite": False,
                            "cells": [
                                [{"value": e["quantity"], "cell_styles": {"number_format": "0"}}]
                                for e in block
                            ],
                        },
                    }
                )
    summary = {
        "expected_cells": len(expected),
        "write": counts["write"],
        "unchanged": counts["unchanged"],
        "needs_review": counts["needs_review"],
        "platform_totals": dict(totals) if not counts["needs_review"] else None,
    }
    LOG.info("销量填充请求生成：operations=%d summary=%s", len(operations), summary)
    return {
        "as_of": report["as_of"],
        "entries": entries,
        "operations": operations,
        "summary": summary,
        "status": "needs_review"
        if counts["needs_review"]
        else "changes_proposed"
        if operations
        else "unchanged",
    }


def verify_sales_update(before: dict, after: dict, update: dict) -> dict:
    """
    功能说明：全量回读核对销量、平台合计及非目标内容，记录公式自动重算。

    参数：
        before：写入前完整快照。
        after：写入后完整快照。
        update：本次核对通过的销量填充请求与逐项结果。

    返回值：校验统计与公式重算记录；内容、样式或结构异常时抛出错误。
    """
    for key in ("spreadsheet_token", "sheet_id", "row_count", "column_count", "merges"):
        if before[key] != after[key]:
            raise ValueError(f"写入后表格身份或结构发生变化：{key}")
    if before["cells"].keys() != after["cells"].keys():
        raise ValueError("回读单元格范围不完整")
    for key in before["layout"]:
        if key != "revision" and before["layout"][key] != after["layout"].get(key):
            raise ValueError(f"写入后布局发生变化：{key}")
    targets = {e["target_cell"]: e for e in update["entries"]}
    totals, recalculated = defaultdict(int), []
    for address, old in before["cells"].items():
        new = after["cells"][address]
        old_rest, new_rest = dict(old), dict(new)
        if address in targets:
            entry = targets[address]
            value = new.get("value")
            if type(value) not in (int, float) or value != entry["quantity"] or new.get("formula"):
                raise ValueError(f"销量回读数量或类型不一致：{address}")
            totals[entry["platform"]] += value
            old_rest.pop("value", None)
            new_rest.pop("value", None)
            if entry["status"] == "write":
                expected_style = {**old.get("cell_styles", {}), "number_format": "0"}
                if new.get("cell_styles") != expected_style:
                    raise ValueError(f"销量单元格样式变化不符合预期：{address}")
                old_rest.pop("cell_styles", None)
                new_rest.pop("cell_styles", None)
        elif old.get("formula"):
            # 仅允许原公式计算结果随输入更新，公式文本及格式必须保留。
            old_value, new_value = old_rest.pop("value", None), new_rest.pop("value", None)
            if old_value != new_value:
                if isinstance(new_value, str) and new_value.startswith("#"):
                    raise ValueError(f"公式重算出现错误：{address} {new_value}")
                recalculated.append(
                    {
                        "cell": address,
                        "formula": old["formula"],
                        "before": old_value,
                        "after": new_value,
                    }
                )
        if old_rest != new_rest:
            raise ValueError(f"单元格内容、公式或格式发生意外变化：{address}")
    if dict(totals) != update["summary"]["platform_totals"]:
        raise ValueError("回读平台销量合计不一致")
    return {
        "verified": True,
        "as_of": update["as_of"],
        "checked_sales_cells": len(targets),
        "checked_all_cells": len(before["cells"]),
        "platform_totals": dict(totals),
        "recalculated_formulas": recalculated,
        "before_revision": before["revision"],
        "after_revision": after["revision"],
    }


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：读取数仓并预览或填充平台近30天销量，保存证据并回读校验。

    参数：
        argv：命令行参数列表；默认读取进程参数。

    返回值：预览或核验通过返回0，运行失败返回1，存在待核对项返回2。
    """
    parser = argparse.ArgumentParser(description="预览或填充市场部近30天发货销量")
    parser.add_argument("--spreadsheet-token", required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--source-config", type=Path, default=Path("config/sales-sources.toml"))
    parser.add_argument("--db-env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    if args.apply and args.expected_revision is None:
        parser.error("执行必须指定 --expected-revision")
    configure_logging("INFO")

    def save(name, data):
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        LOG.info(
            "开始销量填充：sheet_id=%s start=%s end=%s apply=%s",
            args.sheet_id,
            args.as_of - timedelta(days=30),
            args.as_of - timedelta(days=1),
            args.apply,
        )
        config = load_sales_config(args.source_config)
        before = read_sheet(args.spreadsheet_token, args.sheet_id, include_style=True)
        save("before.json", before)
        if args.apply and before["revision"] != args.expected_revision:
            raise ValueError("表格版本与指定版本不一致，请重新预览")
        reader = SalesReader(WarehouseSettings.load(args.db_env_file))
        report = inspect_sales(reader, before, config, args.as_of)
        write_report(args.output, report, before)
        update = build_sales_update(before, report, config)
        save("request.json", update)
        if update["status"] == "needs_review":
            save("result.json", {"status": "needs_review", **update["summary"]})
            LOG.warning("销量存在待核对项，本次不写入：count=%d", update["summary"]["needs_review"])
            return 2
        if not update["operations"]:
            save(
                "result.json",
                {"status": "unchanged", "revision": before["revision"], **update["summary"]},
            )
            LOG.info("销量与数仓一致，无需写入")
            return 0
        save("dry-run.json", _batch(args.spreadsheet_token, update["operations"], dry_run=True))
        if not args.apply:
            save(
                "result.json",
                {"status": "preview", "revision": before["revision"], **update["summary"]},
            )
            LOG.info("销量预览完成：output=%s", args.output)
            return 0
        current = _lark_read("+workbook-info", ["--spreadsheet-token", args.spreadsheet_token])[
            "data"
        ]
        if current["revision"] != before["revision"]:
            raise ValueError("提交前表格版本发生变化，请重新预览")
        save("response.json", _batch(args.spreadsheet_token, update["operations"], dry_run=False))
        after = read_sheet(args.spreadsheet_token, args.sheet_id, include_style=True)
        save("after.json", after)
        result = verify_sales_update(before, after, update)
        save("result.json", result)
        LOG.info(
            "销量填充完成并通过回读：sales_cells=%d totals=%s",
            result["checked_sales_cells"],
            result["platform_totals"],
        )
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, subprocess.SubprocessError) as error:
        LOG.error("销量填充未完成：%s", error)
        save("error.json", {"error": str(error)})
        return 1


def main() -> None:
    raise SystemExit(run())

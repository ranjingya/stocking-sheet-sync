from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from urllib.parse import quote

import httpx

from .layout_compare import comparable_layout, log_height_changes
from .logging_config import configure_logging
from .sales_config import load_sales_config
from .sales_totals import column_total_rows
from .sheet_layout import _bounds, load_layout_config, plan_market_layout
from .sheet_matching import column_name, column_number, inspect_sheet
from .sheets_api import create_client, read_sheet, revision

LOG = logging.getLogger(__name__)


def build_update(snapshot: dict, config: dict, rules: dict, *, forecast: bool = False) -> dict:
    """
    功能说明：为新品及老品生成补齐近30天销量列和市场部合并表头的顺序执行请求。

    参数：
        snapshot：包含完整值、公式、样式和布局的工作表快照。
        config：商品和平台别名配置。
        rules：市场部结构规则。
        forecast：是否补齐老款公式预估的历史、预估与全公司字段。

    返回值：结构预览、服务端操作、原列到新列映射及新增列位置。
    """
    report = plan_market_layout(snapshot, config, rules, recent_only=True, forecast=forecast)
    if report["category"] not in {"new", "legacy"} or report["issues"]:
        raise ValueError("实际执行仅支持结构明确的新品或老品，请先查看结构预览")
    if any(o["action"] == "move_market_column" for o in report["operations"]):
        raise ValueError("平台顺序需要调整，请先核对；补列命令不移动现有平台列")
    if any(f["metric"] == "demand" and not f["source_column"] for f in report["target_fields"]):
        raise ValueError("必须已存在各平台需求列，才能补齐对应销量列")
    if "layout" not in snapshot:
        raise ValueError("执行前必须读取完整布局和样式")
    operations = []
    total_formulas = {}
    inherited_columns = {}
    product_rows = [r["row"] for r in inspect_sheet(snapshot, config)["rows"]]
    sid = snapshot["sheet_id"]

    def add(method, endpoint, **body):
        operations.append({"method": method, "endpoint": endpoint, "body": body})

    def set_value(cell, value):
        add(
            "POST",
            "values_batch_update",
            valueRanges=[{"range": f"{sid}!{cell}:{cell}", "values": [[value]]}],
        )

    inserts = [o for o in report["operations"] if o["action"] == "insert_market_column"]
    mapping = {column_name(c): c for c in range(1, snapshot["column_count"] + 1)}
    group_changes = [o for o in report["operations"] if o["action"] == "set_market_group_header"]
    if group_changes:
        change = group_changes[0]
        if change["before_range"] in snapshot["merges"]:
            add("POST", "unmerge_cells", range=f"{sid}!{change['before_range']}")
    for item in inserts:
        position = column_number(item["position"])
        add(
            "POST",
            "insert_dimension_range",
            dimension={
                "sheetId": sid,
                "majorDimension": "COLUMNS",
                "startIndex": position - 1,
                "endIndex": position,
            },
            inheritStyle="AFTER",
        )
        mapping = {old: new + (new >= position) for old, new in mapping.items()}
    for field in report["target_fields"]:
        if field["source_column"]:
            continue
        demand = next(
            f
            for f in report["target_fields"]
            if (field["platform"] == "company" or f["platform"] == field["platform"])
            and f["metric"] == "demand"
        )
        target = field["target_column"]
        right_source = min(value for value in mapping.values() if value > column_number(target))
        inherited_columns[target] = column_name(right_source)
        for total in column_total_rows(snapshot, demand["source_column"], target, product_rows):
            total_formulas[total["target_cell"]] = total["formula"]
            set_value(total["target_cell"], {"type": "formula", "text": total["formula"]})
        label_width = sum(2 if ord(c) > 127 else 1 for c in field["header"]) * 8 + 16
        add(
            "PUT",
            "dimension_range",
            dimension={
                "sheetId": sid,
                "majorDimension": "COLUMNS",
                "startIndex": column_number(target),
                "endIndex": column_number(target),
            },
            dimensionProperties={"fixedSize": label_width},
        )
    for item in report["operations"]:
        if item["action"] == "set_market_field_header":
            set_value(item["cell"], item["after"])
    if group_changes:
        change = group_changes[0]
        left, top, right, _ = _bounds(change["after_range"])
        # 只清理经过结构规划确认的市场部组表头，保留其他部门内容。
        add(
            "POST",
            "values_batch_update",
            valueRanges=[
                {
                    "range": f"{sid}!{change['after_range']}",
                    "values": [[change["header"], *["" for _ in range(right - left)]]],
                }
            ],
        )
        add("POST", "merge_cells", range=f"{sid}!{change['after_range']}", mergeType="MERGE_ALL")
    LOG.info(
        "近30天补列请求生成：sheet_id=%s inserted=%d operations=%d",
        sid,
        len(inserts),
        len(operations),
    )
    return {
        "forecast": forecast,
        "report": report,
        "operations": operations,
        "column_mapping": {k: column_name(v) for k, v in mapping.items()},
        "inserted_columns": [i["position"] for i in inserts],
        "total_formulas": total_formulas,
        "inherited_columns": inherited_columns,
    }


def verify_update(before: dict, after: dict, update: dict, config: dict, rules: dict) -> dict:
    """
    功能说明：核对新增字段、原内容与样式、行高和合并范围，并验证再次运行无操作。

    参数：
        before：写入前完整快照。
        after：写入后完整快照。
        update：本次顺序执行请求及列映射。
        config：商品与平台别名配置。
        rules：市场部结构规则。

    返回值：核验统计；任一检查失败抛出异常，不尝试自动重写。
    """
    if after["row_count"] != before["row_count"] or after["column_count"] != before[
        "column_count"
    ] + len(update["inserted_columns"]):
        raise ValueError("插列后的行列数量不符合预期")
    for key in ("spreadsheet_token", "sheet_id", "title"):
        if before.get(key) != after.get(key):
            raise ValueError(f"回读表格身份变化：{key}")
    log_height_changes(before["layout"], after["layout"])
    old_layout = comparable_layout(before["layout"])
    new_layout = comparable_layout(after["layout"])
    for key in ("hidden", "sheet_format", "data_validations", "row_dimensions"):
        if old_layout.get(key) != new_layout.get(key):
            raise ValueError(f"原工作表布局发生变化：{key}")
    if "column_dimensions" in before["layout"]:
        for old, new in update["column_mapping"].items():
            if _column_dimension(before, old) != _column_dimension(after, new):
                raise ValueError(f"原列宽、隐藏状态或列样式发生变化：{old}")
    plan = plan_market_layout(
        after, config, rules, recent_only=True, forecast=update.get("forecast", False)
    )
    if plan["status"] != "ready" or plan["operations"]:
        raise ValueError("回读后的市场部结构不符合规则")
    group_row = rules["layout"]["group_row"]
    header_row = config["matching"]["header_rows"]
    original_market = {f["column"] for f in update["report"]["existing_fields"]}
    renamed = {
        f["source_column"]
        for f in update["report"]["target_fields"]
        if f["source_column"] and f["metric"] in {"sales", "forecast", "future"}
    }
    formulas = []
    checked = 0
    for old_col, new_col in update["column_mapping"].items():
        for row in range(1, before["row_count"] + 1):
            old, new = before["cells"][f"{old_col}{row}"], after["cells"][f"{new_col}{row}"]
            if (row == group_row and old_col in original_market) or (
                row == header_row and old_col in renamed
            ):
                continue
            # 插列由飞书调整公式引用；核对公式保留及计算值，记录原式与新式供审阅。
            if old.get("formula"):
                if not new.get("formula") or old.get("value") != new.get("value"):
                    raise ValueError(f"原公式或结果发生异常变化：{old_col}{row}")
                if old["formula"] != new["formula"]:
                    formulas.append(
                        {
                            "before_cell": f"{old_col}{row}",
                            "after_cell": f"{new_col}{row}",
                            "before": old["formula"],
                            "after": new["formula"],
                        }
                    )
            elif old.get("value") != new.get("value") or new.get("formula"):
                raise ValueError(f"原单元格内容发生变化：{old_col}{row}")
            for key in ("cell_styles", "border_styles", "note", "data_validation", "rich_text"):
                if old.get(key) != new.get(key):
                    raise ValueError(f"原单元格格式或备注发生变化：{old_col}{row} {key}")
            checked += 1
    for field in update["report"]["target_fields"]:
        if field["source_column"]:
            continue
        target = field["target_column"]
        demand = next(
            f["target_column"]
            for f in update["report"]["target_fields"]
            if (field["platform"] == "company" or f["platform"] == field["platform"])
            and f["metric"] == "demand"
        )
        for row in range(header_row, after["row_count"] + 1):
            cell = after["cells"][f"{target}{row}"]
            total_formula = update.get("total_formulas", {}).get(f"{target}{row}")
            if total_formula:
                if cell.get("formula") != total_formula or cell.get("value") != 0:
                    raise ValueError(f"新增销量列合计公式或结果不符合预期：{target}{row}")
            elif row > header_row and (cell.get("value") not in (None, "") or cell.get("formula")):
                raise ValueError(f"新增销量列商品行不应带入数量或公式：{target}{row}")
            for key in ("cell_styles", "border_styles"):
                if cell.get(key) != after["cells"][
                    f"{update.get('inherited_columns', {}).get(target, demand)}{row}"
                ].get(key):
                    raise ValueError(f"新增销量列未继承需求列样式：{target}{row}")
    expected_merges = set()
    changed_groups = {
        op["before_range"]
        for op in update["report"]["operations"]
        if op["action"] == "set_market_group_header"
    }
    for area in before["merges"]:
        if area in changed_groups:
            continue
        left, top, right, bottom = _bounds(area)
        expected_merges.add(
            f"{update['column_mapping'][column_name(left)]}{top}:{update['column_mapping'][column_name(right)]}{bottom}"
        )
    expected_merges.update(
        op["after_range"]
        for op in update["report"]["operations"]
        if op["action"] == "set_market_group_header"
    )
    if expected_merges != set(after["merges"]):
        raise ValueError("合并范围不符合插列映射")
    return {
        "verified": True,
        "before_revision": before["revision"],
        "after_revision": after["revision"],
        "checked_original_cells": checked,
        "inserted_columns": update["inserted_columns"],
        "formula_reference_changes": formulas,
        "repeat_operation_count": 0,
    }


def _column_dimension(snapshot: dict, col: str) -> dict:
    """提取 snapshot 中 col 的列尺寸及格式，排除随插列变化的位置属性。"""
    number = column_number(col)
    for dimension in snapshot["layout"]["column_dimensions"].values():
        if int(dimension["min"]) <= number <= int(dimension["max"]):
            return {k: v for k, v in dimension.items() if k not in {"min", "max"}}
    return {}


def apply_update(
    token: str,
    sheet_id: str,
    update: dict,
    expected_revision: int,
    *,
    client=None,
    on_progress=None,
) -> dict:
    """
    功能说明：通过飞书服务端接口顺序执行补列，逐步保存结果并检查版本。

    参数：
        token：目标副本 token。
        sheet_id：工作表 ID。
        update：由 build_update 生成的请求计划。
        expected_revision：执行前已审阅的版本。
        client：可选数据应用客户端；传入时由调用方关闭。
        on_progress：可选回调，每次请求前后保存执行日志。
    返回值：各步骤结果；任一步失败立即停止，已提交步骤不自动重试或回滚。
    """
    owned = client is None
    client = client or create_client()
    journal = {"status": "running", "steps": [], "revision": expected_revision}
    base = f"/open-apis/sheets/v2/spreadsheets/{quote(token, safe='')}"
    try:
        for index, operation in enumerate(update["operations"]):
            if revision(client, token, sheet_id) != journal["revision"]:
                raise ValueError("补列期间版本发生变化，请回读核对，不要直接重试")
            step = {"index": index, "operation": operation, "status": "sending"}
            journal["steps"].append(step)
            if on_progress:
                on_progress(journal)
            LOG.info("补列开始：token=%s step=%d endpoint=%s", token, index, operation["endpoint"])
            step["response"] = client._request(
                operation["method"],
                base + "/" + operation["endpoint"],
                json_body=operation["body"],
                retry=False,
            )
            step["status"] = "acknowledged"
            # 部分结构接口不返回版本；下一步前再次读取，最终以全表核验确认。
            journal["revision"] = revision(client, token, sheet_id)
            if on_progress:
                on_progress(journal)
        journal["status"] = "submitted"
        return journal
    finally:
        if owned:
            client.close()


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：预览或执行新品及老品近30天补列，保存前后快照并进行回读核验。

    参数：
        argv：命令行参数；默认读取进程参数。

    返回值：执行或预览成功返回 0，配置、版本冲突或核验失败返回 1。
    """
    parser = argparse.ArgumentParser(description="预览或执行市场部新品及老品近30天销量补列")
    parser.add_argument("--spreadsheet-token", required=True)
    parser.add_argument("--sheet-id", required=True)
    parser.add_argument("--source-config", type=Path, default=Path("config/sales-sources.toml"))
    parser.add_argument("--layout-config", type=Path, default=Path("config/sheet-layout.toml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-revision", type=int)
    args = parser.parse_args(argv)
    if args.apply and args.expected_revision is None:
        parser.error("执行必须指定 --expected-revision，使用预览读取到的版本")
    configure_logging("INFO")

    def save(name, data):
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        config = load_sales_config(args.source_config)
        rules = load_layout_config(args.layout_config, config)
        before = read_sheet(args.spreadsheet_token, args.sheet_id, include_style=True)
        if args.apply and before["revision"] != args.expected_revision:
            raise ValueError("表格版本与指定版本不一致，请重新预览")
        update = build_update(before, config, rules)
        save("before.json", before)
        save("request.json", update)
        if not update["operations"]:
            save("result.json", {"status": "unchanged", "revision": before["revision"]})
            LOG.info("表头已符合规则，无需插列或写入")
            return 0
        if not args.apply:
            LOG.info("执行预览完成：revision=%s output=%s", before["revision"], args.output)
            return 0
        save(
            "response.json",
            apply_update(
                args.spreadsheet_token,
                args.sheet_id,
                update,
                before["revision"],
                on_progress=lambda journal: save("journal.json", journal),
            ),
        )
        after = read_sheet(args.spreadsheet_token, args.sheet_id, include_style=True)
        save("after.json", after)
        result = verify_update(before, after, update, config, rules)
        save("result.json", result)
        LOG.info("市场部补列完成并通过回读核验：inserted=%d", len(update["inserted_columns"]))
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, httpx.TransportError) as error:
        LOG.error("市场部补列未完成：%s", error)
        save("error.json", {"error": str(error)})
        return 1


def main() -> None:
    raise SystemExit(run())

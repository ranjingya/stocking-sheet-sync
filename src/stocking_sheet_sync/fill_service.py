from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import quote

from .layout_apply import apply_update, build_update, verify_update
from .models import CopyState, FillState
from .sales_config import WarehouseSettings, load_sales_config
from .sales_fill import build_sales_update, verify_sales_update
from .sales_inspect import inspect_sales, write_report
from .sales_reader import SalesReader
from .sheet_layout import load_layout_config, plan_market_layout
from .sheet_matching import column_name, inspect_sheet, normalize_text
from .sheets_api import read_sheet, write_sales_ranges

LOG = logging.getLogger(__name__)


class HistoryFiller:
    def __init__(
        self,
        client,
        *,
        source_path=Path("config/sales-sources.toml"),
        layout_path=Path("config/sheet-layout.toml"),
        reader_factory=None,
        new_history: bool = True,
        legacy_history: bool = True,
    ):
        """
        功能说明：组装副本历史销量填充流程，按当前业务配置定位工作表和数仓来源。

        参数：
            client：飞书数据应用客户端，由调用方管理生命周期。
            source_path：销量及商品匹配配置文件。
            layout_path：市场部表头结构配置文件。
            reader_factory：可选只读数仓客户端工厂，用于隔离测试。
            new_history：是否填写新品历史销量。
            legacy_history：是否填写老款历史销量。
        返回值：无。
        """
        self.new_history = new_history
        self.legacy_history = legacy_history
        self.client = client
        self.source_path = source_path
        self.layout_path = layout_path
        self.reader_factory = reader_factory or (lambda: SalesReader(WarehouseSettings.load()))

    def __call__(self, copy: CopyState, claim: FillState) -> dict:
        """
        功能说明：在固定历史窗口完成检查、补列、销量写入与全量回读，并保存执行证据。

        参数：
            copy：已确认创建成功的副本记录。
            claim：Redis 中已占位的执行记录，包含预估日和本地报告目录。
        返回值：completed、retryable 或 needs_review 状态及原因；写入异常后禁止自动重试。
        """
        output = Path(claim.report_path)
        output.mkdir(parents=True, exist_ok=True)
        writing = False

        def save(name, data):
            (output / name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )

        def finish(status, reason=""):
            result = {
                "status": status,
                "reason": reason,
                "as_of": claim.as_of,
                "target_url": copy.target_url,
            }
            save("result.json", result)
            LOG.info(
                "历史填充结束：target=%s status=%s reason=%s", copy.target_token, status, reason
            )
            return result

        try:
            LOG.info("历史填充开始：target=%s as_of=%s", copy.target_token, claim.as_of)
            config = load_sales_config(self.source_path)
            rules = load_layout_config(self.layout_path, config)
            sid = self.find_sheet(copy.target_token, config)
            before = read_sheet(
                copy.target_token, sid, client=self.client, archive_path=output / "before.xlsx"
            )
            save("before.json", before)
            category = plan_market_layout(before, config, rules, recent_only=True)["category"]
            if (category == "new" and not self.new_history) or (
                category == "legacy" and not self.legacy_history
            ):
                LOG.info("历史填充按款式开关跳过：category=%s", category)
                result = finish("completed", "该款式历史填充开关关闭")
                result["history_status"] = "disabled"
                save("result.json", result)
                return result
            layout_update = build_update(before, config, rules)
            save("layout-request.json", layout_update)
            reader = self.reader_factory()
            report = inspect_sales(reader, before, config, date.fromisoformat(claim.as_of))
            write_report(output / "inspection", report, before)
            allowed = {"sales_column_unresolved", "target_not_empty"}
            problems = {issue for e in report["entries"] for issue in e["issues"]} - allowed
            if problems:
                # 来源覆盖或连接问题允许重试同一窗口；商品歧义需要人工处理。
                catalog_ok = report["summary"]["catalog_matched"] == report["summary"]["sku_rows"]
                status = (
                    "retryable"
                    if catalog_ok and any(s["issues"] for s in report["sources"])
                    else "needs_review"
                )
                return finish(status, "历史检查未通过：" + ", ".join(sorted(problems)))
            for entry in report["entries"]:
                existing = before["cells"].get(entry["target_cell"], {})
                value = existing.get("value")
                if existing.get("formula") or (
                    value not in (None, "")
                    and (type(value) not in (int, float) or value != entry["observed_quantity"])
                ):
                    return finish("needs_review", f"销量单元格已有不同内容：{entry['target_cell']}")
            prepared = before
            if layout_update["operations"]:
                writing = True
                save(
                    "layout-response.json",
                    apply_update(
                        copy.target_token,
                        sid,
                        layout_update,
                        before["revision"],
                        client=self.client,
                        on_progress=lambda journal: save("layout-journal.json", journal),
                    ),
                )
                prepared = read_sheet(
                    copy.target_token,
                    sid,
                    client=self.client,
                    archive_path=output / "prepared.xlsx",
                )
                save("prepared.json", prepared)
                save(
                    "layout-verification.json",
                    verify_update(before, prepared, layout_update, config, rules),
                )
            mapped = remap_report(report, prepared, config)
            update = build_sales_update(prepared, mapped, config)
            save("sales-request.json", update)
            if update["summary"]["needs_review"]:
                return finish("needs_review", "销量或合计存在冲突，详见 sales-request.json")
            after = prepared
            if update["operations"]:
                writing = True
                save(
                    "sales-response.json",
                    write_sales_ranges(
                        copy.target_token,
                        sid,
                        update["operations"],
                        expected_revision=prepared["revision"],
                        client=self.client,
                    ),
                )
                after = read_sheet(
                    copy.target_token, sid, client=self.client, archive_path=output / "after.xlsx"
                )
            save("after.json", after)
            verification = verify_sales_update(prepared, after, update)
            save("sales-verification.json", verification)
            return finish("completed")
        except Exception as error:
            LOG.exception("历史填充异常：target=%s writing=%s", copy.target_token, writing)
            # 只读阶段的外部故障可重试；任何已经开始的写入都保留现场供核验。
            status = (
                "needs_review"
                if writing or isinstance(error, (ValueError, KeyError))
                else "retryable"
            )
            return finish(status, str(error))

    def find_sheet(self, token: str, config: dict) -> str:
        """
        功能说明：根据商品字段与市场部表头选择唯一普通工作表，不依赖工作表名称。

        参数：
            token：目标电子表格 token。
            config：包含表头行数及字段别名的业务配置。
        返回值：唯一候选工作表 ID；没有候选或多个候选时抛错。
        """
        sheets = self.client._request(
            "GET", f"/open-apis/sheets/v3/spreadsheets/{quote(token, safe='')}/sheets/query"
        )["sheets"]
        candidates = []
        matching = config["matching"]
        for sheet in sheets:
            if sheet.get("resource_type", "sheet") != "sheet":
                continue
            cols = sheet["grid_properties"]["column_count"]
            rows = min(sheet["grid_properties"]["row_count"], matching["header_rows"])
            area = f"{sheet['sheet_id']}!A1:{column_name(cols)}{rows}"
            data = self.client._request(
                "GET",
                f"/open-apis/sheets/v2/spreadsheets/{quote(token, safe='')}/values_batch_get",
                params={"ranges": area, "valueRenderOption": "UnformattedValue"},
            )
            if [r["range"] for r in data["valueRanges"]] != [area]:
                raise ValueError("候选工作表表头读取不完整")
            labels = {
                normalize_text(v) for row in data["valueRanges"][0].get("values", []) for v in row
            }
            if all(
                labels & {normalize_text(a) for a in matching[key]}
                for key in (
                    "sku_headers",
                    "style_headers",
                    "name_headers",
                    "spec_headers",
                    "market_headers",
                )
            ):
                candidates.append(sheet["sheet_id"])
        if len(candidates) != 1:
            raise ValueError(f"需要唯一的下单工作表，实际候选：{candidates}")
        return candidates[0]


def remap_report(report: dict, snapshot: dict, config: dict) -> dict:
    """
    功能说明：补列后重定位已核实的销量证据，避免重复查询时历史数据发生漂移。

    参数：
        report：本次执行从数仓获取的原始检查报告。
        snapshot：经全量核验后的工作表快照。
        config：商品与平台表头配置。
    返回值：商品身份不变且坐标已更新的候选报告；身份变化时抛错。
    """
    result = deepcopy(report)
    layout = inspect_sheet(snapshot, config)
    identities = {
        r["row"]: tuple(r[k] for k in ("sku", "style", "name", "spec")) for r in layout["rows"]
    }
    for entry in result["entries"]:
        if identities.get(entry["row"]) != tuple(
            entry[k] for k in ("sku", "style", "name", "spec")
        ):
            raise ValueError("补列前后商品身份发生变化")
        col = layout["columns"][entry["platform"]]["sales"]
        if not col:
            raise ValueError("补列后销量字段仍未定位")
        entry["target_cell"] = f"{col}{entry['row']}"
        entry["issues"] = [
            i for i in entry["issues"] if i not in {"sales_column_unresolved", "target_not_empty"}
        ]
    result["layout"] = layout
    return result

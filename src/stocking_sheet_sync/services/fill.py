from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import quote

from stocking_sheet_sync.domain.models import CopyState, FillState
from stocking_sheet_sync.domain.products import column_name, inspect_sheet, normalize_text
from stocking_sheet_sync.domain.sheets.forecast_values import (
    build_forecast_values,
    check_forecast_target,
    project_layout,
)
from stocking_sheet_sync.domain.sheets.layout import (
    build_update,
    dated_forecast_rules,
    plan_market_layout,
)
from stocking_sheet_sync.domain.sheets.validation import verify_sales_update, verify_update
from stocking_sheet_sync.domain.sheets.values import build_sales_update
from stocking_sheet_sync.infrastructure.feishu.sheets import (
    apply_update,
    read_sheet,
    write_sales_ranges,
)
from stocking_sheet_sync.infrastructure.warehouse import ForecastReader, SalesReader
from stocking_sheet_sync.services.calculation import inspect_forecast, write_forecast_report
from stocking_sheet_sync.services.sales import inspect_sales, write_report
from stocking_sheet_sync.settings import (
    WarehouseSettings,
    load_forecast_config,
    load_forecast_sources,
    load_layout_config,
    load_sales_config,
)

LOG = logging.getLogger(__name__)


class HistoryFiller:
    def __init__(
        self,
        client,
        *,
        reader_factory=None,
        config_path=Path("config/config.toml"),
        new_history: bool = True,
        legacy_history: bool = True,
    ):
        """
        功能说明：组装副本历史销量填充流程，按当前业务配置定位工作表和数仓来源。

        参数：
            config_path：运行配置文件路径，业务规则读取同目录 rules.toml。
            client：飞书数据应用客户端，由调用方管理生命周期。
            reader_factory：可选只读数仓客户端工厂，用于隔离测试。
            new_history：是否填写新品历史销量。
            legacy_history：是否填写老款历史销量。
        返回值：无。
        """
        self.new_history = new_history
        self.legacy_history = legacy_history
        self.client = client
        self.config_path = config_path
        self.reader_factory = reader_factory or (lambda: SalesReader(WarehouseSettings.load()))

    def __call__(self, copy: CopyState, claim: FillState, *, before=None, sid=None) -> dict:
        """
        功能说明：在固定历史窗口完成检查、补列、销量写入与全量回读，并保存执行证据。

        参数：
            copy：已确认创建成功的副本记录。
            claim：Redis 中已占位的执行记录，包含预估日和本地报告目录。
            before：可选已读取的完整快照，用于分流时复用同一版本。
            sid：复用快照对应的工作表标识。
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
            LOG.debug(
                "历史填充结束：target=%s status=%s reason=%s", copy.target_token, status, reason
            )
            return result

        try:
            LOG.info("历史填充开始：target=%s as_of=%s", copy.target_token, claim.as_of)
            config = load_sales_config(self.config_path)
            rules = load_layout_config(self.config_path, config)
            if before is None:
                sid = self.find_sheet(copy.target_token, config)
                before = read_sheet(
                    copy.target_token, sid, client=self.client, archive_path=output / "before.xlsx"
                )
            sid = before["sheet_id"]
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
            if report["summary"]["catalog_matched"] != report["summary"]["sku_rows"]:
                return finish("needs_review", "表内商品未匹配或身份不明确")
            projected = project_layout(before, layout_update, config, rules)
            preflight = build_sales_update(
                projected, remap_report(report, projected, config), config, partial=True
            )
            save("preflight.json", preflight)
            if preflight["summary"]["needs_review"]:
                return finish(
                    "retryable" if any(s["issues"] for s in report["sources"]) else "needs_review",
                    "所有平台历史数据均不可用或目标存在冲突",
                )
            writing = True
            update = self.apply_plan(
                copy,
                sid,
                before,
                layout_update,
                config,
                rules,
                lambda snapshot: build_sales_update(
                    snapshot, remap_report(report, snapshot, config), config, partial=True
                ),
                output,
                save,
                "sales",
            )
            from stocking_sheet_sync.services.notification import summarize_platform_fill

            result = finish("completed")
            result["notification_details"] = summarize_platform_fill(update, config, history=True)
            save("result.json", result)
            return result
        except Exception as error:
            LOG.log(
                logging.DEBUG if isinstance(error, ValueError) else logging.ERROR,
                "历史填充异常：target=%s writing=%s",
                copy.target_token,
                writing,
                exc_info=True,
            )
            # 只读阶段的外部故障可重试；任何已经开始的写入都保留现场供核验。
            status = (
                "needs_review"
                if writing or isinstance(error, (ValueError, KeyError))
                else "retryable"
            )
            return finish(status, str(error))

    def apply_plan(
        self, copy, sid, before, layout, config, rules, build_values, output, save, prefix
    ):
        """
        功能说明：统一执行结构调整、数量写入及全量回读校验。

        参数：
            copy：处理副本的身份与链接。
            sid：目标工作表标识。
            before：写入前的完整快照。
            layout：已完成预检的结构调整计划。
            config：商品和平台来源配置。
            rules：表头与布局规则。
            build_values：根据补列后快照构建数量写入计划的函数。
            output：本批次临时证据目录。
            save：保存JSON证据的函数。
            prefix：历史或预测数量证据的文件名前缀。
        返回值：已写入并核验的数量计划；写入或校验异常向上抛出。
        """
        prepared = before
        if layout["operations"]:
            save(
                "layout-response.json",
                apply_update(
                    copy.target_token,
                    sid,
                    layout,
                    before["revision"],
                    client=self.client,
                    on_progress=lambda journal: save("layout-journal.json", journal),
                ),
            )
            prepared = read_sheet(
                copy.target_token, sid, client=self.client, archive_path=output / "prepared.xlsx"
            )
            save("layout-verification.json", verify_update(before, prepared, layout, config, rules))
        save("prepared.json", prepared)
        update = build_values(prepared)
        save(f"{prefix}-request.json", update)
        if update["summary"]["needs_review"]:
            raise ValueError("补列后数量或合计存在冲突，详见写入请求")
        after = prepared
        if update["operations"]:
            save(
                f"{prefix}-response.json",
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
        save(f"{prefix}-verification.json", verify_sales_update(prepared, after, update))
        return update

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
        rejected = []
        titles = []
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
            missing = [
                " / ".join(matching[key])
                for key in (
                    "sku_headers",
                    "style_headers",
                    "name_headers",
                    "spec_headers",
                    "market_headers",
                )
                if not labels & {normalize_text(a) for a in matching[key]}
            ]
            title = sheet.get("title", sheet["sheet_id"])
            if missing:
                rejected.append(f"工作表「{title}」缺少「{'、'.join(missing)}」表头")
            else:
                candidates.append(sheet["sheet_id"])
                titles.append(title)
        if not candidates:
            raise ValueError("；".join(rejected) or "未找到唯一的下单工作表：没有普通工作表")
        if len(candidates) > 1:
            raise ValueError(f"无法确定唯一的下单工作表：{'、'.join(titles)}均符合条件")
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


class ForecastFiller(HistoryFiller):
    def __init__(
        self,
        client,
        *,
        history: bool,
        new_history: bool = True,
        new_forecast: bool = False,
        legacy_forecast: bool = True,
        reader_factory=None,
        config_path=Path("config/config.toml"),
    ):
        """
        功能说明：组装老款公式预估填充流程，按历史开关控制辅助历史量写入。

        参数：
            config_path：运行配置文件路径，业务规则读取同目录 rules.toml。
            client：飞书服务端接口客户端。
            history：是否同时填充三个历史窗口，关闭时仍读取计算输入。
            new_history：是否填写新品近30天。
            new_forecast：是否请求新品预测；规则未实现时保留空白并标记。
            legacy_forecast：是否填写老款预测。
            reader_factory：可选独立数仓读取器工厂，默认使用本项目配置。
        返回值：无。
        """
        super().__init__(
            client,
            new_history=new_history,
            legacy_history=history,
            config_path=config_path,
            reader_factory=reader_factory or (lambda: ForecastReader(WarehouseSettings.load())),
        )
        self.new_forecast = new_forecast
        self.legacy_forecast = legacy_forecast
        self.history = history

    def __call__(self, copy: CopyState, claim: FillState) -> dict:
        """
        功能说明：查询需求表SKU并核对计算结果，在处理副本补列、写数量并全量回读。

        参数：
            copy：已确认创建的处理副本。
            claim：已占位的执行记录，固定预估日和本地证据目录。
        返回值：执行状态及原因；数据不足不写入，任何写入异常均要求核验。
        """
        output = Path(claim.report_path)
        output.mkdir(parents=True, exist_ok=True)
        writing = False

        def save(name, value):
            (output / name).write_text(
                json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )

        def finish(status, reason=""):
            result = {
                "status": status,
                "reason": reason,
                "as_of": claim.as_of,
                "target_url": copy.target_url,
            }
            save("result.json", result)
            LOG.debug(
                "公式预估填充结束：target=%s status=%s reason=%s", copy.target_token, status, reason
            )
            return result

        try:
            LOG.info("公式预估填充开始：target=%s history=%s", copy.target_token, self.history)
            config = load_sales_config(self.config_path)
            rules = load_layout_config(self.config_path, config)
            sid = self.find_sheet(copy.target_token, config)
            before = read_sheet(
                copy.target_token, sid, client=self.client, archive_path=output / "before.xlsx"
            )
            save("before.json", before)
            category = plan_market_layout(before, config, rules, recent_only=True)["category"]
            if category == "new":
                LOG.info("新品跳过预测：history=%s", self.new_history)
                if self.new_history:
                    result = super().__call__(copy, claim, before=before, sid=sid)
                    result.update(history_status=result["status"], forecast_status="skipped")
                else:
                    result = finish("completed", "新品预测跳过，新品历史填充开关关闭")
                    result.update(history_status="disabled", forecast_status="skipped")
                result["forecast_status"] = "unsupported" if self.new_forecast else "skipped"
                if self.new_forecast:
                    LOG.warning("新品预测规则未实现，保留预测为空并交付历史结果")
                save("result.json", result)
                return result
            if category == "legacy" and not self.legacy_forecast:
                result = super().__call__(copy, claim, before=before, sid=sid)
                result["forecast_status"] = "disabled"
                save("result.json", result)
                return result
            product = inspect_sheet(before, config)
            report = inspect_forecast(
                self.reader_factory(),
                sorted({r["style"] for r in product["rows"]}),
                date.fromisoformat(claim.as_of),
                config,
                load_forecast_sources(self.config_path),
                load_forecast_config(self.config_path),
                rules,
                requested_rows=product["rows"],
                snapshot=before,
            )
            write_forecast_report(output / "inspection", report)
            checked = check_forecast_target(before, report, config)
            save("target-check.json", checked)
            if checked["issues"]:
                return finish("needs_review", "表内商品未匹配或身份不明确，详见 target-check.json")
            rules = dated_forecast_rules(rules, report)
            layout = build_update(before, config, rules, forecast=True)
            save("layout-request.json", layout)
            projected = project_layout(before, layout, config, rules)
            preflight = build_forecast_values(
                projected, report, config, rules, history=self.history, forecast=True, partial=True
            )
            save("preflight.json", preflight)
            if preflight["summary"]["needs_review"]:
                return finish("needs_review", "计算数据不足或目标已有不同内容，详见 preflight.json")
            writing = True
            update = self.apply_plan(
                copy,
                sid,
                before,
                layout,
                config,
                rules,
                lambda snapshot: build_forecast_values(
                    snapshot,
                    report,
                    config,
                    rules,
                    history=self.history,
                    forecast=True,
                    partial=True,
                ),
                output,
                save,
                "values",
            )
            from stocking_sheet_sync.services.notification import summarize_platform_fill

            result = finish("completed")
            result["history_status"] = "completed" if self.history else "disabled"
            result["forecast_status"] = "completed"
            result["notification_details"] = summarize_platform_fill(
                update, config, history=self.history, report=report
            )
            save("result.json", result)
            return result
        except Exception as error:
            LOG.log(
                logging.DEBUG if isinstance(error, ValueError) else logging.ERROR,
                "公式预估填充异常：target=%s writing=%s",
                copy.target_token,
                writing,
                exc_info=True,
            )
            return finish(
                "needs_review"
                if writing or isinstance(error, (ValueError, KeyError))
                else "retryable",
                str(error),
            )

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from stocking_sheet_sync.domain.models import CopyState, FillState
from stocking_sheet_sync.domain.products import inspect_sheet
from stocking_sheet_sync.domain.sheets.forecast_values import (
    build_forecast_values,
    check_forecast_target,
    project_layout,
)
from stocking_sheet_sync.domain.sheets.layout import dated_forecast_rules, plan_market_layout
from stocking_sheet_sync.domain.sheets.values import verify_sales_update
from stocking_sheet_sync.infrastructure.feishu.sheets import read_sheet, write_sales_ranges
from stocking_sheet_sync.infrastructure.forecast_source import ForecastReader
from stocking_sheet_sync.services.calculation import inspect_forecast, write_forecast_report
from stocking_sheet_sync.services.history import HistoryFiller
from stocking_sheet_sync.services.layout import apply_update, build_update, verify_update
from stocking_sheet_sync.settings import (
    WarehouseSettings,
    load_forecast_config,
    load_forecast_sources,
    load_layout_config,
    load_sales_config,
)

LOG = logging.getLogger(__name__)


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
            config_path：统一业务配置文件路径。
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
            LOG.info(
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
                    result = super().__call__(copy, claim)
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
                result = super().__call__(copy, claim)
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
                projected, report, config, rules, history=self.history, forecast=True
            )
            save("preflight.json", preflight)
            if preflight["summary"]["needs_review"]:
                return finish("needs_review", "计算数据不足或目标已有不同内容，详见 preflight.json")
            prepared = before
            if layout["operations"]:
                writing = True
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
                    copy.target_token,
                    sid,
                    client=self.client,
                    archive_path=output / "prepared.xlsx",
                )
                save(
                    "layout-verification.json",
                    verify_update(before, prepared, layout, config, rules),
                )
            save("prepared.json", prepared)
            update = build_forecast_values(
                prepared, report, config, rules, history=self.history, forecast=True
            )
            save("values-request.json", update)
            if update["summary"]["needs_review"]:
                return finish("needs_review", "补列后出现目标冲突，详见 values-request.json")
            after = prepared
            if update["operations"]:
                writing = True
                save(
                    "values-response.json",
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
            save("values-verification.json", verify_sales_update(prepared, after, update))
            return finish("completed")
        except Exception as error:
            LOG.exception("公式预估填充异常：target=%s writing=%s", copy.target_token, writing)
            return finish(
                "needs_review"
                if writing or isinstance(error, (ValueError, KeyError))
                else "retryable",
                str(error),
            )

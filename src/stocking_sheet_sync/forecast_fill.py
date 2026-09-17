from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .fill_service import HistoryFiller
from .forecast import load_forecast_config
from .forecast_inspect import inspect_forecast, write_forecast_report
from .forecast_reader import ForecastReader, load_forecast_sources
from .forecast_sheet import build_forecast_values, check_forecast_target, project_layout
from .layout_apply import apply_update, build_update, verify_update
from .models import CopyState, FillState
from .sales_config import WarehouseSettings, load_sales_config
from .sales_fill import verify_sales_update
from .sheet_layout import load_layout_config
from .sheet_matching import inspect_sheet
from .sheets_api import read_sheet, write_sales_ranges

LOG = logging.getLogger(__name__)


class ForecastFiller(HistoryFiller):
    def __init__(
        self,
        client,
        *,
        history: bool,
        reader_factory=None,
        source_path=Path("config/sales-sources.toml"),
        layout_path=Path("config/sheet-layout.toml"),
        rules_path=Path("config/forecast.toml"),
        forecast_sources_path=Path("config/forecast-sources.toml"),
    ):
        """
        功能说明：组装老款公式预估填充流程，按历史开关控制辅助历史量写入。

        参数：
            client：飞书服务端接口客户端。
            history：是否同时填充三个历史窗口，关闭时仍读取计算输入。
            reader_factory：可选独立数仓读取器工厂，默认使用本项目配置。
            source_path：平台销量配置文件。
            layout_path：市场部表头及款式分类配置文件。
            rules_path：预测季节与分配规则文件。
            forecast_sources_path：预测主数据、日销量和公司来源配置文件。
        返回值：无。
        """
        super().__init__(
            client,
            source_path=source_path,
            layout_path=layout_path,
            reader_factory=reader_factory or (lambda: ForecastReader(WarehouseSettings.load())),
        )
        self.history = history
        self.rules_path = rules_path
        self.forecast_sources_path = forecast_sources_path

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
            config = load_sales_config(self.source_path)
            rules = load_layout_config(self.layout_path, config)
            sid = self.find_sheet(copy.target_token, config)
            before = read_sheet(
                copy.target_token, sid, client=self.client, archive_path=output / "before.xlsx"
            )
            save("before.json", before)
            layout = build_update(before, config, rules, forecast=True)
            save("layout-request.json", layout)
            product = inspect_sheet(before, config)
            report = inspect_forecast(
                self.reader_factory(),
                sorted({r["style"] for r in product["rows"]}),
                date.fromisoformat(claim.as_of),
                config,
                load_forecast_sources(self.forecast_sources_path),
                load_forecast_config(self.rules_path),
                rules,
                requested_rows=product["rows"],
            )
            write_forecast_report(output / "inspection", report)
            checked = check_forecast_target(before, report, config)
            save("target-check.json", checked)
            if checked["issues"]:
                return finish("needs_review", "表内商品未匹配或身份不明确，详见 target-check.json")
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

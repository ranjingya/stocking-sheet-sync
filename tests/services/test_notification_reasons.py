"""通知原因聚合和长度控制的回归检查。"""

from stocking_sheet_sync.services.notification import build_sync_card
from stocking_sheet_sync.services.notification_reasons import compact_reasons


def test_many_sku_failures_keep_periods_without_sku_details():
    detail = "；".join(f"SKU {i:013d}：销量快照缺失" for i in range(100))
    reasons = [
        f"京东自营：近30天（2026-08-30~2026-09-28）：{detail}；去年同期近30天：{detail}；去年后续周期：SKU每日数据不完整，该平台未填充"
    ]
    before = list(reasons)
    result = compact_reasons(reasons)
    assert result == ["京东自营：近30天、去年同期、后续周期数据缺失或不完整，相关数据未填充"]
    assert reasons == before


def test_styles_grouped_and_deduplicated_across_stages():
    reasons = [f"测试平台（款{i}）：去年同期30天基数过低，本次未计算预测" for i in range(30)]
    result = compact_reasons(reasons + reasons)
    assert result == ["测试平台：去年同期30天基数过低，未预测（涉及30款）"]


def test_two_styles_keep_names_and_distinct_causes():
    result = compact_reasons(
        [
            "测试平台（款一）：去年同期30天基数过低，本次未计算预测",
            "测试平台（款二）：去年同期30天基数过低，本次未计算预测",
            "测试平台：ADS 表周期无数据，使用 DWD 明细表填充",
        ]
    )
    assert len(result) == 1
    assert "款一、款二" in result[0]
    assert "使用 DWD 明细表填充" in result[0]


def test_conflicts_and_source_exceptions_do_not_expose_details():
    result = compact_reasons(
        [
            "测试平台：历史单元格已有公式或不同内容（" + "A1、" * 100 + "）",
            "全公司：来源读取失败（SQL SELECT secret FROM database）",
        ]
    )
    assert result == ["测试平台：历史数据不一致，相关款平台已跳过", "全公司：数据读取失败"]


def test_card_keeps_plain_reasons_above_button_and_limits_unrecognized_errors():
    reasons = [f"测试平台：未知错误{i}" + "详情" * 200 for i in range(100)]
    card = build_sync_card(
        original_name="测试",
        record_url="https://example.com/record",
        target_url="https://example.com/result",
        status="success",
        history_status="completed",
        forecast_status="completed",
        details={"history": {"status": "partial", "completed": 1, "total": 2, "reasons": reasons}},
    )
    note = card["body"]["elements"][-2]
    assert len(note["content"]) < 200
    assert "**" not in note["content"]
    assert card["body"]["elements"][-1]["tag"] == "column_set"

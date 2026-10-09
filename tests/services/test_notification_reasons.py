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


def test_generic_permission_advice_is_not_a_permission_failure():
    result = compact_reasons(
        ["天猫超市：去年后续周期：数据来源读取失败，请检查连接、权限或字段配置（错误码 1064）"]
    )
    assert result == ["天猫超市：数据库查询失败，相关数据未填充"]
    assert compact_reasons(
        ["天猫超市：数据来源读取失败，请检查连接、权限或字段配置（错误码 2013）"]
    ) == ["天猫超市：数据读取失败"]


def test_missing_ads_skus_are_business_reason_not_unknown_exception():
    result = compact_reasons(
        [
            "唯品会：近30天：SKU销量缺失或不唯一：6941716574314；去年同期近30天：SKU销量缺失或不唯一：6941716574314，该平台未填充"
        ]
    )
    assert result == ["唯品会：近30天、去年同期数据缺失或不完整，相关数据未填充"]


def test_history_windows_and_forecast_share_one_missing_data_reason():
    reasons = [
        "唯品会（款一）：近30天：SKU销量缺失或不唯一：001，该项未填充",
        "唯品会（款一）：去年同期近30天：SKU销量缺失或不唯一：001，该项未填充",
        "唯品会（款一）：近30天：SKU销量缺失或不唯一：001；去年同期近30天：SKU销量缺失或不唯一：001，本次未计算预测",
    ]
    assert compact_reasons(reasons) == [
        "唯品会：近30天、去年同期数据缺失或不完整，相关历史未填充，未预测（款一）"
    ]


def test_company_keeps_incomplete_style_and_omits_duplicate_source_warning():
    assert compact_reasons(
        [
            "全公司：款一：全公司出库数据不完整，整款未填充",
            "全公司：公司ADS近30天快照缺失",
        ]
    ) == ["全公司：全公司出库数据不完整，整款未填充（款一）"]

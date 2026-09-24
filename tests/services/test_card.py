from stocking_sheet_sync.services.notification import build_sync_card


def test_build_success_card_contains_record_and_target_links() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        target_name="市场部-备货测试表",
        target_url="https://example.feishu.cn/sheets/target-token",
        status="success",
        target_folder_token="folder-token",
    )

    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "下单需求 · 搬运完成"
    assert card["config"]["summary"]["content"] == "下单需求 · 搬运完成"
    content = card["body"]["elements"][0]["content"]
    assert "备货测试表" in content
    assert "record-token" in content
    assert "\n\n" not in content
    actions = card["body"]["elements"][-1]["columns"]
    assert len(actions) == 1
    assert actions[0]["elements"][0]["behaviors"][0]["default_url"].endswith("target-token")


def test_build_failure_card_contains_reason() -> None:
    card = build_sync_card(
        original_name="备货测试表",
        record_url="https://example.feishu.cn/record/record-token",
        status="failure",
        target_folder_token="folder-token",
        reason="没有访问权限",
    )

    assert card["header"]["template"] == "red"
    assert "没有访问权限" in card["body"]["elements"][-2]["content"]

    assert "未生成" not in card["body"]["elements"][0]["content"]


def test_partial_forecast_and_reason_are_displayed_without_blank_lines():
    from stocking_sheet_sync.services.notification import summarize_forecast

    config = {"platforms": [{"id": "a", "name": "甲平台"}, {"id": "b", "name": "乙平台"}]}
    report = {
        "groups": [
            {
                "style": "style",
                "platforms": [
                    {"platform": "a", "status": "ready", "issues": []},
                    {"platform": "b", "status": "manual", "issues": ["previous_sales_zero"]},
                ],
            }
        ]
    }
    details = summarize_forecast(report, config)
    card = build_sync_card(
        original_name="测试表",
        record_url="https://example.feishu.cn/record/source",
        status="success",
        target_url="https://example.feishu.cn/sheets/target",
        history_status="completed",
        forecast_status="completed",
        details=details,
    )
    assert card["header"]["template"] == "orange"
    text = card["body"]["elements"][0]["content"]
    assert "**历史数据：**✅ 已填充" in text
    assert "部分完成（1/2平台全部完成）" in text
    assert "原因" not in text
    note = card["body"]["elements"][-2]
    assert "原因：乙平台：去年同期销量为0" in note["content"]
    assert note["text_size"] == "notation"
    assert "**原始记录：**" in text
    assert "\n\n" not in text


def test_multi_style_platform_is_complete_only_when_all_styles_are_ready():
    from stocking_sheet_sync.services.notification import summarize_forecast

    report = {
        "groups": [
            {"style": "s1", "platforms": [{"platform": "a", "status": "ready"}]},
            {
                "style": "s2",
                "platforms": [
                    {
                        "platform": "a",
                        "status": "manual",
                        "issues": ["company_lifecycle_sales_unavailable"],
                    }
                ],
            },
        ]
    }
    result = summarize_forecast(report, {"platforms": [{"id": "a", "name": "甲"}]})["forecast"]
    assert result["status"] == "partial" and result["completed"] == 0
    assert "甲（s2）" in result["reasons"][0]
    assert "全公司生命周期销量" in result["reasons"][0]


def test_disabled_unsupported_and_degraded_states_have_reasons():
    common = dict(
        original_name="测试",
        record_url="https://example.feishu.cn/record/r",
        status="success",
        target_url="https://example.feishu.cn/sheets/t",
    )
    card = build_sync_card(**common, history_status="disabled", forecast_status="unsupported")
    text = card["body"]["elements"][0]["content"]
    assert "**历史数据：**未开启" in text and "**预测：**暂不支持" in text
    note = card["body"]["elements"][-2]["content"]
    assert "开关关闭" in note and "新品预测暂不支持" in note
    card = build_sync_card(
        **common,
        history_status="completed",
        forecast_status="completed",
        degraded=True,
        reason="写入失败",
    )
    text = card["body"]["elements"][0]["content"]
    assert "已填充" not in text and "已计算" not in text
    assert "已交付未填充原表" in card["body"]["elements"][-2]["content"]


def test_all_manual_forecast_does_not_report_completed():
    from stocking_sheet_sync.services.notification import summarize_forecast

    details = summarize_forecast(
        {
            "groups": [
                {
                    "style": "s",
                    "platforms": [
                        {"platform": "a", "status": "manual", "issues": ["previous_sales_zero"]}
                    ],
                }
            ]
        },
        {"platforms": [{"id": "a", "name": "甲"}]},
    )
    card = build_sync_card(
        original_name="测试",
        record_url="https://example.feishu.cn/record/r",
        status="success",
        target_url="https://example.feishu.cn/sheets/t",
        history_status="disabled",
        forecast_status="completed",
        details=details,
    )
    assert "**预测：**未计算" in card["body"]["elements"][0]["content"]
    assert "待人工处理" in card["header"]["title"]["content"]


def test_platform_reasons_are_separate_lines_above_button():
    card = build_sync_card(
        original_name="测试",
        record_url="https://example.com/record",
        target_url="https://example.com/result",
        status="success",
        history_status="completed",
        forecast_status="completed",
        details={
            "forecast": {
                "status": "partial",
                "completed": 1,
                "total": 3,
                "reasons": ["甲平台：去年同期为0", "乙平台：缺少公司销量"],
            }
        },
    )
    note = card["body"]["elements"][-2]
    assert "甲平台：去年同期为0\n乙平台：缺少公司销量" in note["content"]
    assert "**" not in note["content"]
    assert card["body"]["elements"][-1]["tag"] == "column_set"


def test_ads_detail_fallback_is_reported_under_the_platform():
    from stocking_sheet_sync.services.notification import summarize_platform_fill

    config = {"platforms": [{"id": "pdd", "name": "拼多多"}, {"id": "vip", "name": "唯品会"}]}
    source = {
        "platform": "pdd",
        "issues": [],
        "fallback": {"reason": "ads_snapshot_day_empty"},
    }
    details = summarize_platform_fill(
        {}, config, history=True, history_report={"sources": [source]}
    )
    assert details["history"]["status"] == "completed"
    assert details["history"]["reasons"] == ["拼多多：ADS 表周期无数据，使用 DWD 明细表填充"]
    card = build_sync_card(
        original_name="测试",
        record_url="https://example.com/record",
        target_url="https://example.com/result",
        status="success",
        history_status="completed",
        forecast_status="disabled",
        details=details,
    )
    assert (
        "拼多多：ADS 表周期无数据，使用 DWD 明细表填充" in (card["body"]["elements"][-2]["content"])
    )


def test_forecast_only_notification_reports_valid_ads_detail_fallback():
    from stocking_sheet_sync.services.notification import summarize_platform_fill

    config = {"platforms": [{"id": "vip", "name": "唯品会"}]}
    report = {
        "groups": [
            {
                "style": "KQ25001",
                "platforms": [
                    {
                        "platform": "vip",
                        "status": "ready",
                        "issues": [],
                        "sources": {
                            "current": {
                                "platform": "vip",
                                "issues": [],
                                "fallback": {"reason": "ads_snapshot_day_empty"},
                            }
                        },
                    }
                ],
            }
        ]
    }
    details = summarize_platform_fill({}, config, history=False, report=report)
    assert details["forecast"]["status"] == "completed"
    assert details["forecast"]["reasons"] == ["唯品会：ADS 表周期无数据，使用 DWD 明细表填充"]

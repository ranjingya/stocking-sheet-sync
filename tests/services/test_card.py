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
    assert "**原因：**乙平台：去年同期销量为0" in note["content"]
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

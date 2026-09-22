# Stocking Sheet Sync

下单需求表格填充。由飞书多维表 Webhook 触发，查询需求表中的SKU，读取数仓销量，按配置填写市场部历史数据和老品公式预估。

每次处理生成三份在线文件：原始备份、处理备份、交付表。填充失败时交付原始备份；Redis 保存批次、阶段状态与去重结果。

## 启动

需要 Python 3.12+、uv、Redis，以及飞书和数仓访问权限。

```bash
uv sync
cp .env.example .env
cp config/config.example.toml config/config.toml
```

填写 `.env` 中的凭证和连接信息，在 `config/config.toml` 中设置来源、目录和业务规则。

```bash
uv run gunicorn -c gunicorn.py 'stocking_sheet_sync.entrypoints.web:create_app()'
```

Docker 启动与 Webhook 接入见 [运行指南](docs/operations.md)。

## 填充开关

根目录 `.env` 提供四个独立开关，示例配置如下：

```dotenv
FILL_OLD_HISTORY_ENABLED=true
FILL_OLD_FORECAST_ENABLED=false
FILL_NEW_HISTORY_ENABLED=true
FILL_NEW_FORECAST_ENABLED=false
```

新品只支持近30天历史。新品预测规则尚未实现；开启对应开关时报告“规则未实现”，不套用老品公式。新老品混合在同一工作表时需人工核验。

## 常用命令

```bash
# 所有子命令及参数
uv run stocking-sheet-sync --help

# 强制创建一个新的搬运填充批次
uv run stocking-sheet-sync rerun --record-id rec_xxx

# 使用相同请求标识恢复该批次
uv run stocking-sheet-sync rerun --record-id rec_xxx --request-id batch_xxx

# 只读试算指定老款，结果写入本地目录
uv run stocking-sheet-sync forecast --style KQ25073 --as-of 2026-09-04 --output artifacts/trial
```

`inspect`、`forecast`、`layout` 用于诊断；`layout-apply`、`sales-fill` 用于指定版本的表格操作；`notify` 用于显式手动通知。各命令使用 `--help` 查看参数，业务配置参数统一为 `--config`。

## 文档

- [架构与流程](docs/architecture.md)
- [业务规则](docs/business-rules.md)
- [配置说明](docs/configuration.md)
- [运行与排错](docs/operations.md)
- [验证与支持边界](docs/verification.md)

## 开发检查

```bash
uv run --group lint ruff check src tests
uv run pytest -q
```

源码按入口、应用服务、领域规则、基础设施分层。测试使用配置模板和外部接口替身，不需要实际凭证，不会写入在线业务表。

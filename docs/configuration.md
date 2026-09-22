# 配置说明

运行目录为项目根目录。业务配置默认读取 `config/config.toml`，完整模板为 `config/config.example.toml`。`.env` 与实际配置不提交，也不进入Docker镜像。

## 环境变量

| 配置 | 用途 |
| --- | --- |
| `FEISHU_DATA_APP_ID`、`FEISHU_DATA_APP_SECRET` | 读取多维表、复制与填充表格的数据应用 |
| `FEISHU_MESSAGE_APP_ID`、`FEISHU_MESSAGE_APP_SECRET` | 发送通知的消息应用 |
| `WEBHOOK_SECRET` | Webhook Bearer认证 |
| `REDIS_URL` | Redis连接 |
| `WAREHOUSE_HOST`、`WAREHOUSE_PORT`、`WAREHOUSE_DATABASE` | 数仓地址、端口和库名 |
| `WAREHOUSE_USER`、`WAREHOUSE_PASSWORD` | 数仓只读凭证 |
| `WAREHOUSE_CONNECT_TIMEOUT`、`WAREHOUSE_READ_TIMEOUT` | 数仓连接和读取超时 |

数仓凭证仅从当前项目 `.env` 与环境变量读取，环境变量优先。不借用其他项目的连接代码或配置。

## 四项填充开关

| 环境变量 | 作用 | 模板值 |
| --- | --- | --- |
| `FILL_OLD_HISTORY_ENABLED` | 老品历史销量 | true |
| `FILL_OLD_FORECAST_ENABLED` | 老品公式预测 | false |
| `FILL_NEW_HISTORY_ENABLED` | 新品近30天 | true |
| `FILL_NEW_FORECAST_ENABLED` | 新品预测，当前规则未实现 | false |

缺省开关为false，接受true/false、1/0、yes/no、on/off，非法值拒绝启动。

老品仅历史开启时填近30天；仅预测开启时读取历史作为输入、只填预测；同时开启时填当前30天、去年同期30天、去年后续周期和预测。新品历史独立控制，新品预测关闭时跳过；开启时标记规则未实现，历史仍可交付。

兼容 `FILL_HISTORY_ENABLED` 和 `FILL_FORECAST_ENABLED` 两个老品别名；显式 `FILL_OLD_*` 优先。普通重复触发复用原批次开关，需要重新处理时创建新批次。

## TOML分区

| 分区 | 内容 |
| --- | --- |
| `feishu` | 飞书接口地址 |
| `source` | 多维表、表格链接字段、触发记录筛选条件 |
| `target` | 备份目录、交付目录、命名前缀 |
| `notifications` | 成功与失败通知接收人 |
| `redis` | 去重命名空间 |
| `web` | 对外HTTPS地址 |
| `runtime` | 超时、重试、锁、日志与临时文件策略 |
| `catalog` | 唯一商品主数据来源、字段与筛选 |
| `matching` | SKU、款号、名称、规格的表头别名 |
| `sheet` | 市场部结构、新老品年份、平台顺序、预估表头 |
| `forecast` | 季节截止日、兜底阈值、公司销量列识别 |
| `platforms.<平台>` | 数仓表、字段、筛选、去重键、表头别名与布局 |

京东平台还包含 `daily_fields`、`rolling_fields` 和业务日期偏移。平台来源与表头在同一平台分区维护，商品主数据只配置一处。

## 文件目录与清理

当前本地测试配置：

- 备份：`T9vzf7RRUlvgGBdMvmjcC1IPnnb`。
- 交付测试：`TaT1fz5PMl5fjMdpWxGc5PN0nQh`。

实际目录以运行环境的配置为准。示例文件使用占位值，不包含生产凭证。

```toml
[runtime]
fill_report_dir = "artifacts/fill"
temp_max_files = 100
temp_max_bytes = 1073741824
```

每次搬运结束后，清理该专用目录中的Excel、JSON、CSV等文件；按修改时间从旧到新删除，直到同时满足100个文件和1 GiB上限。不跟随符号链接，不删除飞书备份或Redis状态。被清理的本地报告不能继续作为审阅证据。独立诊断命令指定的其他输出目录由使用者管理。

## Docker目录映射

```yaml
volumes:
  - /home/yatui/stocking-sheet-sync/config:/app/config:ro
  - /home/yatui/stocking-sheet-sync/artifacts:/app/artifacts
```

无需额外配置目录环境变量。宿主机 `.env` 通过Compose的 `env_file` 注入，修改后需重新创建容器。

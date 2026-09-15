# Stocking Sheet Sync

下单需求表格填充项目。由多维表自动化触发，将源电子表格复制到指定共享文件夹，按环境变量开关执行市场部历史销量填充，并发送包含各阶段状态的结果卡片。

## 工作方式

1. 多维表自动化将触发记录的 `record_id` 发送给 Webhook。
2. 服务读取该记录，检查 `source.required_fields`，解析配置的链接字段，支持直接 Sheet 链接和 Wiki 节点。
3. 按“记录 ID + 真实电子表格 token”查询 Redis。已有成功副本时复用目标表格；同一记录更换为另一张源表格时可以搬运。
4. 在 Redis 写入永久的 `copying` 占位后，复制表格到目标文件夹，副本名为配置前缀加源名称。
5. 保存目标 token、名称、链接和搬运时间，将搬运状态设为 `copied`。
6. 根据 `.env` 的两个开关处理历史与预测阶段。历史开启时，先核对商品及数仓覆盖，再补齐新品销量列、填写近30天销量并全量回读。
7. 保存阶段结果并发送卡片。副本已生成但填充未完成时，降级为仅搬运，成功卡片展示副本链接、填充状态和具体原因。

源表格的内容修改不会触发追加副本。程序通过 Webhook 处理指定记录。

## 环境与授权

- Python 3.12 或更高版本、uv、Redis 6 或更高版本。
- 数据应用：读取多维表记录、解析 Wiki 节点、读取源表格并复制文件到目标共享文件夹，以及读取、编辑和导出目标电子表格。
- 消息应用：启用机器人能力并发布，允许向通知接收人发送消息。

数据应用需要实际获得多维表、源文件及目标文件夹权限。多维表启用高级权限时，应为应用分配可读取记录的角色。通知人员必须在消息应用可用范围内，且使用消息应用对应的 `open_id`。

## 安装与配置

```bash
uv sync
cp .env.example .env
cp config/config.example.toml config/config.toml
```

在项目根目录运行程序，主配置默认读取 `config/config.toml`，历史销量来源配置默认读取 `config/sales-sources.toml`。`config/sheet-layout.toml` 提供市场部新品、老品结构规则。三份 TOML 分别维护搬运设置、数仓来源和市场部表头结构。

`.env` 保存飞书应用凭证、`REDIS_URL`、`WEBHOOK_SECRET`、`FILL_HISTORY_ENABLED`、`FILL_FORECAST_ENABLED` 和历史销量使用的 `WAREHOUSE_*` 数仓参数。`.env` 与实际主配置 `config/config.toml` 由 Git 和 Docker 构建上下文忽略；配置示例、销量来源和市场部结构规则随项目维护。

| 配置区块 | 用途 |
| --- | --- |
| `feishu` | 飞书 OpenAPI 地址 |
| `source` | 多维表、链接字段、搬运条件；`required_fields = {}` 表示不按字段过滤 |
| `target` | 目标文件夹及副本名前缀 |
| `notifications` | `open_ids` 接收成功通知，`failure_open_ids` 接收失败通知 |
| `redis` | 去重记录命名空间 `key_prefix` |
| `web` | Webhook 对外 HTTPS 地址 |
| `runtime` | 锁有效期、接口超时、重试次数、日志级别及填充报告目录 |

`runtime.lock_ttl_seconds` 默认为 300 秒，只控制并发锁。去重记录与复制占位永久保存。`runtime.max_retries` 默认为 3，适用于读取等接口；复制和表格写入请求只发送一次。

`INFO` 日志记录每条记录的处理结果、副本 token 和链接、通知结果、状态加载及异常。可通过 `openssl rand -hex 32` 生成 Webhook 密钥并写入 `.env`。

## 自动填充开关

在 `.env` 中分别设置：

```dotenv
FILL_HISTORY_ENABLED=true
FILL_FORECAST_ENABLED=false
```

| 开关 | 开启后的行为 |
| --- | --- |
| `FILL_HISTORY_ENABLED` | 读取本项目数仓来源，补齐新品市场部销量列并填入近30天历史数据 |
| `FILL_FORECAST_ENABLED` | 请求预测阶段；当前返回 `unsupported`，说明预测规则尚未实现，需求数量保持现有内容 |

两个开关独立。均关闭时只搬运；只开预测时不会读取数仓或补历史列。未配置的开关默认关闭，示例配置为历史开启、预测关闭。支持 `true/false`、`1/0`、`yes/no`、`on/off`，大小写不敏感，非法值会阻止启动。开关控制 Webhook 自动流程；显式执行的历史检查、补列及填充命令按其命令参数运行。

自动历史填充以副本创建时的上海日期为预估日，使用该日前30个完整自然日的发货数据。重试固定使用同一日期，数仓缺数据时等待该窗口数据补全，不自动改用更早窗口。工作表根据商品字段和市场部表头定位，要求唯一候选；当前自动填充支持新品，老品及结构不明确的表格返回待核验。

`summary.history_status` 和 `summary.forecast_status` 分别展示阶段状态，`target_url` 指向保留的副本，`fill_report_path` 指向本次报告。填充未完成时，`fill_degraded=true`，整体按搬运成功返回 HTTP 200：本次创建副本为 `copied`，复用已有副本为 `unchanged`，`failed=0`；成功通知中说明降级原因。搬运本身失败仍返回 `failed`。降级保留现有副本；若已有部分写入，不自动清除或回滚，阶段状态及报告用于后续核验。已完成历史填充的重复触发会直接跳过该阶段，保留运营后续修改。

历史执行报告默认保存至 `artifacts/fill/<attempt_id>/`。可在 `config/config.toml` 的 `[runtime]` 设置 `fill_report_dir`。报告包含输入快照、数仓匹配证据、逐步补列日志、销量请求及全量回读核验结果。

## Webhook 服务

```bash
uv run gunicorn \
  --bind 127.0.0.1:8000 \
  --workers 2 \
  --threads 2 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  'stocking_sheet_sync.web:create_app()'
```

多维表自动化中的“发送 HTTP 请求”节点填写：

```text
请求方式：POST
请求地址：https://<服务域名>/webhooks/base-record
请求头：Authorization: Bearer <WEBHOOK_SECRET>
请求头：Content-Type: application/json
```

请求体中的 `record_id` 使用触发记录的“记录 ID”变量：

```json
{"record_id": "recxxxxxxxxxxxx"}
```

| result | 含义 | HTTP 状态码 |
| --- | --- | --- |
| `copied` | 已完成搬运 | 200 |
| `unchanged` | 该记录的源表格已有成功副本 | 200 |
| `skipped` | 记录条件不符或链接不受支持 | 200 |
| `busy` | 搬运锁已被占用，可稍后重试 | 409 |
| `failed` | 处理失败，详见 `reason` 和日志 | 500 |

`GET /healthz` 用于进程健康检查。

## Redis 去重与异常处理

每个源表格对应一个永久 Redis String：

```text
<key_prefix>:<record_id>:<source_token>
```

JSON 包含源记录及源表格信息、`status`、本次操作的 `attempt_id` 和 `started_at`，成功后包含 `target_token`、`target_name`、`target_url`、`copied_at`。时间以 UTC+8 保存。并发锁使用 `<key_prefix>:lock:scan`，带有效期且只允许持有者释放。

服务启动时会将仍存在的历史成功搬运记录转换为永久去重记录，以其最新目标副本作为搬运结果。已过期或丢失的记录无法自动恢复。损坏记录保留供核对，对应源表格的请求会失败。

飞书明确拒绝复制时，服务释放本次占位，可以在解决权限等问题后重新触发。复制超时、服务端异常、成功响应缺少文件信息，或复制后的 Redis 写入失败时，服务保留 `copying` 占位。再次触发会返回失败并要求核对，避免生成重复副本。

处理长期停留在 `copying` 的记录时：

1. 确认对应请求已结束，暂停该记录的自动化触发。
2. 根据日志、目标文件夹及副本内容核对复制是否成功。
3. 已成功时，将原状态中的 `status` 设为 `copied`，补齐目标 token、名称、链接及 `copied_at`，永久保存；明确未生成副本时，删除该条占位后重新触发。
4. 恢复自动化触发。

成功状态先于通知保存。通知失败写日志，后续 Webhook 会按成功记录去重；需要补发时使用手动通知命令。

历史填充另存一个永久键 `<key_prefix>:<record_id>:<source_token>:history`，包含目标 token、固定预估日、执行凭证、报告路径及状态：

| 状态 | 再次触发时的行为 |
| --- | --- |
| `running` | 已占位或执行结果待确认，阻止另一任务写入 |
| `completed` | 直接跳过，保留现有内容 |
| `retryable` | 在同一副本、同一窗口重新检查；只读阶段的来源缺口或连接失败可进入此状态 |
| `needs_review` | 停止自动执行，根据报告核对冲突或不确定的写入结果 |

关闭历史开关时不会创建或修改历史状态。已有搬运副本后开启历史开关，会在原副本上启动历史填充。全局运行锁即使过期，同一副本的永久填充占位仍阻止重复执行。

处理 `running` 或 `needs_review` 时，应先确认旧任务已退出，再结合报告与实际表格核对每一步。核对完成后，可将该历史键标记为 `completed`；确认结构完整且允许重新核对空白目标时，可改为 `retryable` 并保留原预估日。不要删除搬运键来重试填充。

Redis 应开启持久化并配置备份，避免淘汰这些键。去重依赖 Redis 状态的保留；清空或丢失记录后，程序无法识别已有副本。

## 手动补发搬运通知

以下命令向 `notifications.open_ids` 发送绿色成功卡片，只处理通知：

```bash
uv run stocking-sheet-sync-notify \
  --original-name '原始记录名称' \
  --original-url 'https://example.feishu.cn/record/record-token' \
  --target-name '市场部-目标表格名称' \
  --target-url 'https://example.feishu.cn/sheets/target-token'
```

## Docker Compose

`docker-compose.yaml` 运行 `stocking-sheet-sync` Webhook 服务，使用外部 Redis，通过 Traefik 提供 HTTPS 接口。宿主机按以下结构准备配置：

```text
/home/yatui/stocking-sheet-sync/
├── docker-compose.yaml
├── .env
├── artifacts/
└── config/
    ├── config.toml
    ├── sales-sources.toml
    └── sheet-layout.toml
```

将 `config/config.example.toml` 复制为宿主机的 `config/config.toml` 并填写业务设置，将项目的 `config/sales-sources.toml` 和 `config/sheet-layout.toml` 放入同一目录。Compose 将整个宿主机目录 `/home/yatui/stocking-sheet-sync/config` 只读挂载到容器的 `/app/config`；该目录需要包含上述三份 TOML。容器工作目录为 `/app`，程序按默认相对路径读取配置，凭证通过 `.env` 注入。

在部署目录启动：

```bash
docker compose pull stocking-sheet-sync
docker compose up -d --remove-orphans stocking-sheet-sync
```

`artifacts` 目录以可写方式挂载到 `/app/artifacts`，执行证据在容器重建后仍保留。

宿主机修改 TOML 后，执行 `docker compose restart stocking-sheet-sync` 使常驻服务加载配置；手动检查命令每次启动时读取销量来源配置。更新配置无需构建镜像。修改 `.env` 后，执行 `docker compose up -d --force-recreate stocking-sheet-sync` 重新注入环境变量。

CD 发布镜像并更新 Compose 文件，宿主机的 `.env` 和 `config/` 由部署人员维护。

## 检查与测试

```bash
uv run pytest
uv run --group lint ruff check .
```

测试使用隔离的状态存储和 HTTP 模拟响应，不连接业务 Redis 或飞书。

## 历史销量检查

`stocking-sheet-sync-inspect` 按指定日期读取五个平台的发货数据，核对商品及目标列，输出本地 JSON、CSV。使用方法、来源、去重与日期覆盖规则见 [历史发货数据读取与表格匹配](docs/sales-data.md)。

## 市场部结构准备

`stocking-sheet-sync-layout` 按款号分类，预览市场部新增列、标题和分组调整，输出 Markdown 与 JSON。支持线上表格和离线完整快照，不连接数仓或修改表格。`stocking-sheet-sync-layout-apply` 支持为新品实际补齐销量列、继承需求列样式及调整市场部合并表头；默认预览，实际执行需指定版本，并在写入后回读核验。使用方法与字段规则见 [市场部结构准备](docs/market-layout.md)。

## 历史销量填充

`stocking-sheet-sync-sales-fill` 使用飞书服务端 API 和数据应用认证，读取本项目配置的数仓来源，在市场部商品行填充近30天销量。必须指定预估日，默认预览；实际写入需加 `--apply --expected-revision`。支持商品与覆盖检查、非空冲突保护、相同值跳过、批量写入和全量回读，日期缺口不会作为零销量填入。使用方法见 [历史销量读取与填充](docs/sales-data.md#实际填充近30天销量)。

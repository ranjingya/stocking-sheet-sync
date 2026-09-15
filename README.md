# Stocking Sheet Sync

下单需求表格填充项目。由多维表自动化触发，将源电子表格复制到指定共享文件夹，按环境变量开关执行市场部历史销量填充，并发送包含各阶段状态的结果卡片。

## 工作方式

1. 多维表自动化将触发记录的 `record_id` 发送给 Webhook。
2. 服务读取该记录，检查 `source.required_fields`，解析配置的链接字段，支持直接 Sheet 链接和 Wiki 节点。
3. 按“记录 ID + 真实电子表格 token”查询 Redis。已有成功副本时复用目标表格；同一记录更换为另一张源表格时可以搬运。
4. 创建批次占位，冻结备份目录、交付目录、填充开关及批次名称。
5. 将源文件复制到备份文件夹，作为原始备份；再从原始备份复制一份处理备份。
6. 在处理备份上按开关执行历史与预测阶段。历史填充先核对商品与数据覆盖，再补列、填写销量并回读核验。
7. 保存填充结果及交付来源。填充成功时复制处理备份到目标文件夹；填充失败时复制原始备份到目标文件夹，半成品留在备份目录供核验。
8. 保存交付结果并通知。两份备份名称使用相同批次时间，通知和返回值包含原始备份、处理备份及交付链接。

源表格的内容修改不会触发追加副本。程序通过 Webhook 处理指定记录。

## 环境与授权

- Python 3.12 或更高版本、uv、Redis 6 或更高版本。
- 数据应用：读取多维表记录、解析 Wiki 节点、读取源表格并复制文件到目标共享文件夹，以及读取、编辑和导出目标电子表格。
- 消息应用：启用机器人能力并发布，允许向通知接收人发送消息。

数据应用需要实际获得多维表、源文件、备份文件夹及目标文件夹权限。多维表启用高级权限时，应为应用分配可读取记录的角色。通知人员必须在消息应用可用范围内，且使用消息应用对应的 `open_id`。

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
| `target` | `backup_folder_token` 为备份目录，`folder_token` 为交付目录，`copy_name_prefix` 为交付文件名前缀 |
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

两个开关独立。均关闭时只搬运；只开预测时不会读取数仓或补历史列。未配置的开关默认关闭，示例配置为历史开启、预测关闭。支持 `true/false`、`1/0`、`yes/no`、`on/off`，大小写不敏感，非法值会阻止启动。开关控制 Webhook 和手动重新搬运流程；显式执行的历史检查、补列及填充命令按其命令参数运行。

自动历史填充以原始备份创建时的上海日期为预估日，使用该日前30个完整自然日的发货数据。数仓缺数据时降级交付原始备份，不自动改用更早窗口。工作表根据商品字段和市场部表头定位，要求唯一候选；当前自动填充支持新品，老品及结构不明确的表格返回待核验。

`summary.history_status` 和 `summary.forecast_status` 分别展示阶段状态，`target_url` 指向保留的副本，`fill_report_path` 指向本次报告。填充未完成时，`fill_degraded=true`，整体按搬运成功返回 HTTP 200：本次创建副本为 `copied`，复用已有副本为 `unchanged`，`failed=0`；成功通知中说明降级原因。搬运本身失败仍返回 `failed`。降级时保留处理备份中的已有写入，交付文件从原始备份创建。`original_backup_url` 和 `filled_backup_url` 分别指向两份备份，`delivery_source` 为 `original` 或 `filled`。交付决定保存后，重复触发只继续未完成的复制步骤，不重新填充、不更换交付来源；需要重新计算时用手动命令创建新批次。

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

## 手动重新搬运

在项目根目录执行一次手动搬运填充：

```bash
uv run stocking-sheet-sync-rerun --record-id recxxxxxxxxxxxx
```

命令自动生成独立批次，按当前源文件重新搬运，并遵循正常的搬运条件、历史与预测开关、填充失败降级流程。旧副本和旧任务记录保留；每个新批次创建三份文件，历史窗口按原始备份创建日期确定。执行一次后退出，不监听 HTTP，也不需要启动 Webhook 服务。程序读取同一份 `.env` 和 `config/config.toml`，使用同一个 Redis 并发锁，并向配置的接收人发送正常结果通知；不需要 `WEBHOOK_SECRET`。

启动时日志会显示 `request_id` 和可直接复制的重试命令。重试同一批次时使用该标识，例如：

```bash
uv run stocking-sheet-sync-rerun --record-id recxxxxxxxxxxxx --request-id rerun-001
```

省略 `--request-id` 表示主动创建新批次，不应将此命令直接作为失败自动重试命令。沿用相同标识时复用副本，已完成填充直接跳过；复制结果不确定时保留占位并阻止重复复制。同一标识绑定的源文件 token 不允许改变。标识支持 1 至 128 位字母、数字、下划线或短横线。

| 入口 | 启动命令 | 行为 |
| --- | --- | --- |
| Webhook 服务 | `uv run gunicorn -c gunicorn.py 'stocking_sheet_sync.web:create_app()'` | 常驻运行，接收自动化发来的记录 ID，按普通规则去重 |
| 手动重新搬运 | `uv run stocking-sheet-sync-rerun --record-id recxxxxxxxxxxxx` | 立即处理指定记录的新批次，完成后退出 |

Webhook 请求体只用于普通搬运；包含 `force` 或 `request_id` 会返回 HTTP 400，提示使用手动命令。手动批次与普通任务独立去重，不覆盖普通任务状态。两种入口共享同一套搬运填充流程。

命令向标准输出写入 JSON 结果，包含副本链接、填充状态、降级原因、`force` 和 `request_id`，日志写到标准错误。退出码：0 表示搬运成功、降级为仅搬运或因条件不符跳过；1 表示处理失败；2 表示参数错误；3 表示已有任务占用运行锁。

## Redis 去重与异常处理

每个源表格对应一个永久 Redis String：

```text
<key_prefix>:<record_id>:<source_token>
```

JSON 包含源记录及源表格信息、`status`、本次操作的 `attempt_id` 和 `started_at`，成功后包含 `target_token`、`target_name`、`target_url`、`copied_at`。时间以 UTC+8 保存。并发锁使用 `<key_prefix>:lock:scan`，带有效期且只允许持有者释放。

服务启动时会将仍存在的历史成功搬运记录转换为永久去重记录，以其最新目标副本作为搬运结果。已过期或丢失的记录无法自动恢复。损坏记录保留供核对，对应源表格的请求会失败。

三份文件的批次使用 `workflow=triple`。批次键下另存以下永久记录：

| 后缀 | 内容 |
| --- | --- |
| `:step:original` | 原始备份的复制来源、文件夹、占位和结果 |
| `:step:filled` | 处理备份的复制占位和结果 |
| `:history` | 处理备份的历史填充状态及固定窗口 |
| `:outcome` | 冻结的填充结果和交付来源 |
| `:step:delivery` | 目标文件夹交付副本的复制占位和结果 |

飞书明确拒绝复制时只释放该步骤占位，重试复用此前已完成的文件。响应不确定或复制后状态保存失败时保留步骤占位，需核对后再处理，不能直接删除整个批次。交付步骤已成功而批次状态未保存时，重试仅补齐批次结果，不再次复制。

`running` 填充状态表示仍在执行或结束结果待确认，阻止另一任务提前交付。已明确结束的填充失败可进入降级流程，冻结交付原始备份；后续数据补全也不会自动修改已交付批次。

已有单份副本的历史任务继续按原记录去重；需要三份文件时，通过手动重新搬运创建新批次。

成功状态先于通知保存。通知失败写日志，后续 Webhook 会按成功记录去重；需要补发时使用手动通知命令。

强制批次使用永久键 `<key_prefix>:force:<record_id>:<request_id>`，保存与普通任务相同的搬运信息，并记录 `request_id`。强制批次的历史填充键为 `<key_prefix>:force:<record_id>:<request_id>:history`。每批分别占位，运行锁仍由所有任务共享。

填充状态包括 `running`（执行或待确认）、`completed`（已完成）、`retryable`（来源暂不可用）、`needs_review`（需人工核验）。三份文件批次以 `:outcome` 中的结果决定交付内容；一旦保存决定，不再自动重跑填充。各阶段记录永久保留，与短期运行锁分开。

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

## 预测规则

预测业务口径、数据缺口、待确认参数及实施顺序见 [市场部下单需求预测规则与实施计划](docs/forecast-rules.md)。

## 文件命名

批次时间使用任务开始时的上海时间，格式为 `YYYYMMDD-HHMMSS`，在创建批次时保存。

| 文件用途 | 名称 |
| --- | --- |
| 原始备份 | `原表名-批次时间-原始备份` |
| 填充备份 | `原表名-批次时间-填充完成` |
| 填充未完成的备份 | `原表名-批次时间-填充未完成` |
| 两个填充开关均关闭的备份 | `原表名-批次时间-未填充` |
| 目标文件夹交付 | `市场部-原表名`，前缀由 `target.copy_name_prefix` 配置 |

处理备份创建时标为“填充未完成”，结果确定后通过服务端接口更新标题并回读核验。
改名失败时保留备份及填充结果，同批次重试只继续改名和交付，不重新填充或复制备份。
同批次重试沿用首次保存的时间和名称，已完成任务不会重新改名。历史批次沿用其保存的名称。
强制重搬创建独立批次；交付文件可以同名，去重使用记录、源文件 token 和手动批次标识，不使用文件名。

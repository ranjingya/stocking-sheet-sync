# Stocking Sheet Sync

下单需求表格填充项目。支持历史发货数据读取与商品、行列映射检查，以及飞书表格的一次性搬运：由多维表自动化触发，将源电子表格复制到指定共享文件夹，并发送结果卡片。

## 工作方式

1. 多维表自动化将触发记录的 `record_id` 发送给 Webhook。
2. 服务读取该记录，检查 `source.required_fields`，解析配置的链接字段，支持直接 Sheet 链接和 Wiki 节点。
3. 按“记录 ID + 真实电子表格 token”查询 Redis。已有成功记录时返回 `unchanged`；同一记录更换为另一张源表格时可以搬运。
4. 在 Redis 写入永久的 `copying` 占位后，复制表格到目标文件夹，副本名为配置前缀加源名称。
5. 保存目标 token、名称、链接和搬运时间，将状态设为 `copied`，然后发送绿色成功卡片。

源表格的内容修改不会触发追加副本。程序通过 Webhook 处理指定记录。

## 环境与授权

- Python 3.12 或更高版本、uv、Redis 6 或更高版本。
- 数据应用：读取多维表记录、解析 Wiki 节点、读取源表格并复制文件到目标共享文件夹。
- 消息应用：启用机器人能力并发布，允许向通知接收人发送消息。

数据应用需要实际获得多维表、源文件及目标文件夹权限。多维表启用高级权限时，应为应用分配可读取记录的角色。通知人员必须在消息应用可用范围内，且使用消息应用对应的 `open_id`。

## 安装与配置

```bash
uv sync
cp .env.example .env
cp config.example.toml config.toml
```

`.env` 保存飞书应用凭证、`REDIS_URL`、`WEBHOOK_SECRET`，可用 `CONFIG_PATH` 指定 TOML 配置位置。配置文件与凭证文件均应保存在 Git 管理范围之外。

| 配置区块 | 用途 |
| --- | --- |
| `feishu` | 飞书 OpenAPI 地址 |
| `source` | 多维表、链接字段、搬运条件；`required_fields = {}` 表示不按字段过滤 |
| `target` | 目标文件夹及副本名前缀 |
| `notifications` | `open_ids` 接收成功通知，`failure_open_ids` 接收失败通知 |
| `redis` | 去重记录命名空间 `key_prefix` |
| `web` | Webhook 对外 HTTPS 地址 |
| `runtime` | 锁有效期、接口超时、重试次数及日志级别 |

`runtime.lock_ttl_seconds` 默认为 300 秒，只控制并发锁。去重记录与复制占位永久保存。`runtime.max_retries` 默认为 3，适用于读取等接口；复制请求只发送一次。

`INFO` 日志记录每条记录的处理结果、副本 token 和链接、通知结果、状态加载及异常。可通过 `openssl rand -hex 32` 生成 Webhook 密钥并写入 `.env`。

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

`docker-compose.yaml` 运行 `stocking-sheet-sync` Webhook 服务，使用外部 Redis，通过 Traefik 提供 HTTPS 接口。准备好镜像、`.env` 和 `config.toml` 后启动：

```bash
docker compose pull stocking-sheet-sync
docker compose up -d --remove-orphans stocking-sheet-sync
```

## 检查与测试

```bash
uv run pytest
uv run --group lint ruff check .
```

测试使用隔离的状态存储和 HTTP 模拟响应，不连接业务 Redis 或飞书。

## 历史销量检查

`stocking-sheet-sync-inspect` 按指定日期读取五个平台的发货数据，核对商品及目标列，输出本地 JSON、CSV。使用方法、来源、去重与日期覆盖规则见 [历史发货数据读取与表格匹配](docs/sales-data.md)。

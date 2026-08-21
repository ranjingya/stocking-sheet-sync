# Stocking Sheet Sync

将飞书多维表中的备货电子表格同步到指定共享文件夹，并向配置的人员发送成功或失败卡片。

## 工作方式

程序由 Webhook 服务和定时 Worker 共同完成同步：

1. 多维表自动化将新增或修改记录的 `record_id` 发送给 Webhook。
2. Webhook 解析“下单表格”字段，兼容 Wiki 链接和直接 Sheet 链接，并立即创建首次副本。
3. 已监听表格再次触发 Webhook 时只进入静默观察，不立即创建更新副本。
4. 每张表格使用一个带三自然日有效期的 Redis String 保存监听状态和全部搬运版本。
5. Worker 每 30 分钟检查状态合格且记录仍指向同一表格的监听对象。
6. 发现变化的表格进入每分钟一次的静默观察；revision 再次变化时重新计时。
7. revision 连续 10 分钟不变后再次校验记录状态，创建新副本并发送通知。

Worker 只监控 Redis 中已经登记且未过期的表格，不扫描多维表历史记录。同一 revision
最多复制一次；程序不会自动删除历史副本。

首次副本保持 `市场部-原标题`；后续更新副本按成功搬运次数依次命名为 `市场部-原标题-v2`、
`市场部-原标题-v3`。源标题带 `.xlsx` 等表格扩展名时，版本号位于扩展名前，例如
`市场部-原标题-v2.xlsx`。

首次同步成功卡片使用绿色主题，更新同步成功卡片使用黄色主题，失败卡片使用红色主题。
所有卡片都提供原始记录和目标共享文件夹入口，成功卡片还提供本次同步副本入口。

## 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Redis 6 或更高版本
- 两个飞书企业自建应用：数据应用和消息应用

数据应用负责读取和复制资源，需要具备以下能力，并实际获得对应资源的访问权限：

- 读取目标多维表记录
- 读取 Wiki 节点信息
- 读取电子表格元信息
- 复制云空间文件到目标文件夹

消息应用负责通知，需要具备以下能力：

- 以应用机器人身份发送消息

还需要完成以下资源授权：

- 将数据应用加入目标多维表；启用高级权限时，为数据应用分配可读取记录的角色。
- 确保数据应用可以读取源 Wiki/电子表格。
- 确保数据应用可以向目标共享文件夹写入文件。
- 为消息应用启用机器人能力并发布，让通知接收人在消息应用可用范围内。

`open_id` 与应用有关。`notifications.open_ids` 必须使用消息应用获取的
`open_id`，不能直接复用集成平台生成的 `anycross_user_*` ID。

## 安装

```bash
uv sync
cp .env.example .env
cp config.example.toml config.toml
```

配置分为两部分：

- `.env`：保存两套飞书应用凭证、`REDIS_URL` 和 Webhook 密钥。
- `config.toml`：保存多维表、目标文件夹、通知人员和运行参数。

`config.toml` 已加入 `.gitignore`，可以直接填写实际业务参数；项目提交的是
`config.example.toml` 模板。如需将配置放在其他位置，在 `.env` 中设置 `CONFIG_PATH`。

配置区块说明：

- `[source]`：源多维表、链接字段、监听状态条件和监听自然日数。
- `[target]`：目标共享文件夹及副本名前缀。
- `[notifications]`：逐人通知使用的 `open_id` 数组。
- `[redis]`：Redis 键命名空间。
- `[runtime]`：常规检查间隔、变动检查间隔、静默时间、超时、重试和日志级别。
- `[web]`：Webhook 对外 HTTPS 地址。

`runtime.log_level = "INFO"` 适用于生产环境，只记录服务启动、Webhook 收到与处理结果、
定时扫描发现的变化以及异常。设置为 `DEBUG` 时会额外记录接口请求、解析、去重、复制和
通知等排障明细。

生成 Webhook 密钥并写入 `.env`：

```bash
openssl rand -hex 32
```

```dotenv
WEBHOOK_SECRET=上一步生成的随机字符串
```

## 多维表自动化 Webhook

项目使用 Flask 接收多维表自动化请求，线上由 Gunicorn 运行：

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

当前服务地址和接口为：

```text
https://stock-sync.kktree.cn/webhooks/base-record
```

多维表自动化中的“发送 HTTP 请求”节点填写：

```text
请求方式：POST
请求地址：https://stock-sync.kktree.cn/webhooks/base-record
请求头：Authorization: Bearer <WEBHOOK_SECRET>
请求头：Content-Type: application/json
```

请求体中的 `record_id` 选择触发记录的“记录 ID”变量：

```json
{
  "record_id": "recxxxxxxxxxxxx"
}
```

Webhook 只读取并处理指定记录。以下地址用于负载均衡器或部署平台健康检查：

```text
GET https://stock-sync.kktree.cn/healthz
```

本机测试 Webhook：

```bash
set -a
source .env
set +a

curl --request POST 'http://127.0.0.1:8000/webhooks/base-record' \
  --header "Authorization: Bearer $WEBHOOK_SECRET" \
  --header 'Content-Type: application/json' \
  --data '{"record_id":"recxxxxxxxxxxxx"}'
```

Gunicorn 负责即时接收新增或修改记录。常驻 Worker 只检测 Redis 中已登记电子表格的
`revision` 变化；两种入口共享 Redis 锁和同步状态，不会重复搬运同一版本。

## 运行

执行一轮 Redis 已登记表格检查：

```bash
uv run stocking-sheet-sync --once
```

启动常驻 Worker：

```bash
uv run stocking-sheet-sync
```

也可以直接运行：

```bash
uv run python main.py --once
```

## Redis 状态

每个已监听表格保存在一个 Redis String：

```text
ss:<record_id>:<source_token>
```

Value 是 JSON，包含：

- 多维表记录 ID、真实电子表格 token、名称和链接
- 原多维表记录链接
- 最新 revision、搬运版本号、目标副本及 UTC+8 同步时间
- 首次监听时间、过期时间和静默观察状态
- `versions`：全部成功搬运版本及其目标链接

Key 在首次同步日期后的第三个自然日零点过期，后续搬运不会延长有效期。

复制失败时不写 Redis，下次扫描会继续尝试。通知失败只写日志，不额外保存通知状态。

扫描锁保存为带自动过期时间的 Redis String：

```text
ss:lock:scan
```

可以通过以下命令查看同步记录：

```bash
redis-cli -u "$REDIS_URL" --scan --pattern 'ss:*'
redis-cli -u "$REDIS_URL" GET 'ss:<record_id>:<source_token>'
```

Redis 需要开启持久化或使用可靠的托管实例。清空这些键会让程序失去历史去重依据。

## Docker Compose

`docker-compose.yaml` 使用同一个镜像运行两个长期服务：

- `stocking-sheet-sync`：Gunicorn Webhook 服务，通过 Traefik 对外提供 HTTPS 接口。
- `stocking-sheet-sync-worker`：单进程定时 Worker，不开放端口。

两个服务共用 `.env`、`config.toml` 和 Redis。启动或更新服务：

```bash
docker compose up -d --force-recreate
```

## 检查与测试

```bash
uv run pytest
uv run --group lint ruff check .
```

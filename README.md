# Stocking Sheet Sync

将飞书多维表中的备货电子表格同步到指定共享文件夹，并向配置的人员发送成功或失败卡片。

## 工作方式

程序由 Webhook 服务和定时 Worker 共同完成同步：

1. 多维表自动化将新增或修改记录的 `record_id` 发送给 Webhook。
2. Webhook 解析“下单表格”字段，兼容 Wiki 链接和直接 Sheet 链接，并立即创建首次副本。
3. 首次复制成功后，将 `record_id + sheet_token + revision` 写入 Redis。
4. Worker 每 5 分钟从 Redis 读取已经成功同步过的表格，并检查在线 `revision`。
5. 发现变化的表格进入每分钟一次的静默观察；revision 再次变化时重新计时。
6. revision 连续 5 分钟不变后创建新副本、保存新版本并发送通知。

Worker 只监控 Redis 中已经登记的表格，不扫描多维表历史记录。同一 revision 最多复制一次；
程序不会自动删除历史副本。

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

- `[source]`：源多维表、视图、链接字段和记录筛选条件。
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

所有已同步表格版本保存在一个 Redis Hash：

```text
stocking-sheet-sync:synced
```

Hash field 是同步版本唯一标识：

```text
<record_id>:<source_token>:<revision>
```

Hash value 是 JSON，只保存：

- 源表格名称和链接
- 原多维表记录链接
- 目标副本名称和链接
- UTC+8 同步时间

表格处于静默观察期时，最新同步版本的 value 还会保存：

- `pending_revision`：观察中的在线 revision
- `pending_since`：该 revision 最近一次变化的 UTC+8 时间

复制失败时不写 Redis，下次扫描会继续尝试。通知失败只写日志，不额外保存通知状态。

扫描锁保存为带自动过期时间的 Redis String：

```text
stocking-sheet-sync:lock:scan
```

可以通过以下命令查看同步记录：

```bash
redis-cli -u "$REDIS_URL" HGETALL 'stocking-sheet-sync:synced'
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

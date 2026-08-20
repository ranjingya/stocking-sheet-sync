# Stocking Sheet Sync

将飞书多维表中的备货电子表格同步到指定共享文件夹，并向配置的人员发送成功或失败卡片。

## 工作方式

程序按固定间隔扫描多维表记录：

1. 读取配置视图中的记录并应用字段过滤条件。
2. 解析“下单表格”字段，兼容 Wiki 链接和直接 Sheet 链接。
3. 获取真实电子表格的 `revision`。
4. 将 `record_id + sheet_token + revision` 与 Redis 状态比较。
5. 首次发现或 revision 变化时，将电子表格复制到共享文件夹。
6. 保存复制结果，再逐人发送飞书卡片。

同一 revision 最多复制一次。源表内容发生变化并产生新 revision 后，会再次创建一个副本；程序不会自动删除历史副本。

## 环境要求

- Python 3.12 或更高版本
- [uv](https://docs.astral.sh/uv/)
- Redis 6 或更高版本
- 一个飞书企业自建应用

飞书应用需要具备以下能力，并实际获得对应资源的访问权限：

- 读取目标多维表记录
- 读取 Wiki 节点信息
- 读取电子表格元信息
- 复制云空间文件到目标文件夹
- 以应用机器人身份发送消息

还需要完成以下资源授权：

- 将应用加入目标多维表；启用高级权限时，为应用分配可读取记录的角色。
- 确保应用可以读取源 Wiki/电子表格。
- 确保应用可以向目标共享文件夹写入文件。
- 启用机器人能力、发布应用，并让通知接收人在应用可用范围内。

`open_id` 与应用有关。`notifications.open_ids` 必须使用当前自建应用获取的
`open_id`，不能直接复用集成平台生成的 `anycross_user_*` ID。

## 安装

```bash
uv sync
cp .env.example .env
cp config.example.toml config.toml
```

配置分为两部分：

- `.env`：保存 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和 `REDIS_URL` 等连接凭证。
- `config.toml`：保存多维表、目标文件夹、通知人员和运行参数。

`config.toml` 已加入 `.gitignore`，可以直接填写实际业务参数；项目提交的是
`config.example.toml` 模板。如需将配置放在其他位置，在 `.env` 中设置 `CONFIG_PATH`。

配置区块说明：

- `[source]`：源多维表、视图、链接字段和记录筛选条件。
- `[target]`：目标共享文件夹及副本名前缀。
- `[notifications]`：逐人通知使用的 `open_id` 数组。
- `[redis]`：Redis 键命名空间。
- `[runtime]`：扫描间隔、超时、重试和日志级别。

## 首次接管

如果现有记录已经由旧集成平台搬运过，先建立当前 revision 基线，避免全部重复搬运：

```bash
uv run stocking-sheet-sync --baseline
```

基线命令只记录 Redis 中尚不存在的表格版本。

如果需要让程序首次启动时搬运所有符合条件的记录，跳过基线命令即可。

## 运行

执行一轮扫描：

```bash
uv run stocking-sheet-sync --once
```

按 `runtime.poll_interval_minutes` 常驻轮询：

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

## 检查与测试

```bash
uv run pytest
uv run --group lint ruff check .
```

# 运行与排错

## Docker运行

准备宿主机 `config/config.toml`、`config/rules.toml`、`.env`、`artifacts` 和 `logs` 目录；确认配置对应的数仓、Redis及飞书目录可访问。仓库Compose使用镜像标签，代码修改需要构建/发布对应镜像后才能在服务器生效。

```bash
docker compose up -d
docker compose logs -f stocking-sheet-sync
```

单个应用容器内运行一个应用进程，Waitress接收HTTP请求，后台线程串行处理任务，使用已有Redis；服务监听5000端口，Compose通过Traefik提供HTTPS。配置目录默认映射到 `/app/config`。本地开发可运行：

```bash
uv run stocking-sheet-sync serve
```

## Webhook

多维表自动化发送 `POST /webhooks/base-record`，请求头为 `Authorization: Bearer <WEBHOOK_SECRET>` 和 `Content-Type: application/json`。健康检查使用 `GET /healthz`。

```json
{"record_id":"rec_xxx"}
```

成功入队返回HTTP 202，例如 `{"status":"accepted","task_id":"123-0","record_id":"rec_xxx"}`；这是接收确认，最终结果通过原有通知和日志查看。入队失败返回503，可重新触发；每次重新发送请求都会产生独立批次；客户端自动重发同样会产生新批次。`/healthz`仅表示Web进程可响应，不代表后台消费和外部依赖均可用。

Webhook自动创建独立批次，不接受手动批次参数。真实业务自动化由使用者选择测试记录触发，首先交付到测试目录。

## 本地Webhook测试

在项目目录运行 `uv run stocking-sheet-sync serve`，同一个终端查看接收与处理日志。另开终端发送请求，替换记录ID：

```bash
uv run python - <<'PYTHON'
import httpx
from stocking_sheet_sync.settings import load_config

config = load_config()
response = httpx.post(
    "http://127.0.0.1:5000/webhooks/base-record",
    headers={"Authorization": f"Bearer {config.webhook_secret}"},
    json={"record_id": "rec_测试记录ID"},
    timeout=30,
)
print(response.status_code, response.text)
PYTHON
```

202表示入队成功。任务实际执行复制、填充和配置中的通知；测试时使用测试目录和接收人。相同记录再次触发也会独立搬运和填充；可重复使用同一条测试记录。按Ctrl+C停止服务，等待当前任务结束。

## 手动重搬

```bash
docker compose exec stocking-sheet-sync uv run stocking-sheet-sync rerun --record-id rec_xxx
```

命令输出 `request_id`。同一次操作遇到中断或失败，用同一标识恢复：

```bash
docker compose exec stocking-sheet-sync uv run stocking-sheet-sync rerun --record-id rec_xxx --request-id batch_xxx
```

每次省略标识都会创建新批次。源表内容更新但token不变时，也使用手动重搬。

## 诊断入口

```bash
uv run stocking-sheet-sync inspect --help
uv run stocking-sheet-sync forecast --help
uv run stocking-sheet-sync layout --help
uv run stocking-sheet-sync layout-apply --help
uv run stocking-sheet-sync sales-fill --help
uv run stocking-sheet-sync notify --help
```

只读诊断可以指定快照或历史日期；自动服务的基准日固定为批次原始备份创建日。写入诊断命令要求显式执行参数和版本号，防止覆盖已变化的表格。

## 常见结果

- 已完成：交付填写后的处理备份。
- 填充降级：交付原始备份，处理备份保留现场，查看原因和阶段状态。
- 已搬运：同一任务恢复时复用该批次交付链接。
- 忙碌：等待运行中的任务结束；重搬使用相同请求标识。
- 结果未确认：先核对实际文件与Redis阶段占位，不直接删除状态重试。
- 新品预测规则未实现：不套用老品公式，按新品历史开关处理。

## 权限与排查

数据应用需要读取多维表、源表、复制目录，并具备电子表格读取、编辑和导出权限。通知接收人的open_id必须属于消息应用身份，且在可用范围内。

INFO日志显示任务开始、复制结果、填充失败原因和最终交付链接；填充失败时明确标记交付未填充原表。`runtime.log_level = "DEBUG"` 可查看队列、锁续期、规则加载和阶段状态等排查细节。临时目录中的报告有数量和容量保留上限，长期证据以飞书原始/处理备份为准。

后台串行执行，运行锁默认300秒，每100秒自动续期。后台连接异常自动重试；容器进程退出后由Docker重启，未确认任务优先恢复。容器停止时后台线程最多等待300秒完成当前任务，Compose停止宽限期为330秒；超时终止留下的未确认任务在启动后恢复，结果未知的写入仍按原有占位规则核验。

执行失败默认最多3次、间隔10秒，锁忙等待不消耗次数。终态任务结果保留7天，业务去重永久保存。Redis应配置适合运行环境的持久化策略；不要手动裁剪未完成队列。手动 `rerun` 仍直接执行并等待结果，与后台线程共享业务运行锁。


## 日志保留

控制台和 `logs/app.log` 同步输出日志。每个任务用开始、结束分隔，记录批次标识和总耗时；INFO保留业务进度及异常，DEBUG提供逐SKU、字段映射和接口细节。

每个日志文件达到10 MiB后滚动，保留5份旧文件（`app.log.1` 至 `app.log.5`）。服务器目录映射为 `/home/yatui/stocking-sheet-sync/logs:/app/logs`，容器重建后仍可查看。

```bash
tail -f logs/app.log
```

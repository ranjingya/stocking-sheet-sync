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
docker compose exec stocking-sheet-sync /app/.venv/bin/stocking-sheet-sync rerun --record-id rec_xxx
```

命令输出 `request_id`。同一次操作遇到中断或失败，用同一标识恢复：

```bash
docker compose exec stocking-sheet-sync /app/.venv/bin/stocking-sheet-sync rerun --record-id rec_xxx --request-id batch_xxx
```

每次省略标识都会创建新批次。源表内容更新但token不变时，也使用手动重搬。

## 指定表格原地填充

```bash
# 只填历史
docker compose exec stocking-sheet-sync /app/.venv/bin/stocking-sheet-sync fill --url "https://kocotree.feishu.cn/sheets/表格token" --history
# 只填预测
docker compose exec stocking-sheet-sync /app/.venv/bin/stocking-sheet-sync fill --url "https://kocotree.feishu.cn/sheets/表格token" --forecast
# 同时填写
docker compose exec stocking-sheet-sync /app/.venv/bin/stocking-sheet-sync fill --url "https://kocotree.feishu.cn/sheets/表格token" --history --forecast
```

直接修改指定表格，不复制、不通知；只操作市场部字段，保留已有不同内容和公式。命令参数决定填充项目，不受自动流程的新老品开关影响；新品预测仍未支持。默认采用上海当天日期，可用 `--as-of YYYY-MM-DD` 指定基准日。通过链接的 `sheet` 参数或 `--sheet-id` 选择工作表，否则自动识别唯一的下单工作表。

与后台任务共用运行锁，忙碌时退出码为3；完成为0，部分完成或待核对为2，执行异常为1。成功回读后清理临时文件，未完成报告按容量上限保留。历史日期的ADS快照不可用时对应平台保持待核对，不替换统计日期。

CLI包含 `serve`、`rerun`、`fill` 三个入口；`serve`用于容器启动服务，日常手动操作使用后两者。通过 `命令 --help` 查看参数。

## 常见结果

- 已完成：交付填写后的处理备份。
- 填充降级：交付原始备份，处理备份保留现场，查看原因和阶段状态。
- 已搬运：同一任务恢复时复用该批次交付链接。
- 忙碌：等待运行中的任务结束；重搬使用相同请求标识。
- 结果未确认：先核对实际文件与Redis阶段占位，不直接删除状态重试。
- 新品预测规则未实现：不套用老品公式，按新品历史开关处理。

## 权限与排查

数据应用需要读取多维表、源表、复制目录，并具备电子表格读取、编辑和导出权限。通知接收人的open_id必须属于消息应用身份，且在可用范围内。

INFO日志显示任务开始、复制结果、填充失败原因和最终交付链接；填充失败时明确标记交付未填充原表。`runtime.log_level = "DEBUG"` 可查看队列、锁续期、规则加载和阶段状态等排查细节。验证通过且交付完成后删除本次临时目录；失败或降级的现场按100个文件和1 GiB上限保留。日志独立保留，长期证据以飞书原始/处理备份为准。

后台串行执行，运行锁默认300秒，每100秒自动续期。后台连接异常自动重试；容器进程退出后由Docker重启，未确认任务优先恢复。容器停止时后台线程最多等待300秒完成当前任务，Compose停止宽限期为330秒；超时终止留下的未确认任务在启动后恢复，结果未知的写入仍按原有占位规则核验。

执行失败默认最多3次、间隔10秒，锁忙等待不消耗次数。终态任务结果保留7天，业务去重永久保存。Redis应配置适合运行环境的持久化策略；不要手动裁剪未完成队列。手动 `rerun` 仍直接执行并等待结果，与后台线程共享业务运行锁。


## 日志保留

控制台和 `logs/app.log` 同步输出日志。每个任务用开始、结束分隔，记录批次标识和总耗时；INFO保留业务进度及异常，DEBUG提供逐SKU、字段映射和接口细节。

每个日志文件达到10 MiB后滚动，保留5份旧文件（`app.log.1` 至 `app.log.5`）。服务器目录映射为 `/home/yatui/stocking-sheet-sync/logs:/app/logs`，容器重建后仍可查看。

```bash
tail -f logs/app.log
```


## 结果通知

卡片保留原始记录链接、历史与预测状态，以及一个结果表格按钮；正文各行连续排列。原始记录、历史数据和预测各占一行，标题加粗；未完成原因在状态下方以灰色小字显示，按钮位于卡片最底部，每个平台原因单独一行，不展示备份和文件夹链接。

- 全部完成：历史显示已填充、预测显示已计算。
- 部分完成：显示全部完成的平台数；同一平台包含多个款式时，所有款式完成才计入该数。
- 人工判断：去年同期为零或公司生命周期销量不可用的预测保留原内容，并说明对应平台和款式。
- 开关关闭：显示未开启；新品预测不支持时明确说明。
- 填充降级：显示仅搬运完成，说明交付的是未填充原表。
- 搬运失败：显示失败原因；没有结果链接时按钮指向原始记录。
- 手动通知未提供填充结果：显示未核验，不推断填写成功。

平台摘要随批次和填充状态保存，恢复任务不依赖本地临时报告。历史和预测按平台隔离：来源数据缺失或目标数量冲突时，该平台数量及合计保持原内容，其余平台继续填充并交付，卡片显示部分完成及平台原因。部分平台异常的临时报告按容量上限保留。商品身份、共享表格结构异常或写入核验失败时，整份表降级为仅搬运；全部平台不可用时同样降级。

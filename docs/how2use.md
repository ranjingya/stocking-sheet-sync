**本地 CLI 使用说明**

先进入项目目录：

```bash
cd /Users/ranjyaa/code/stocking-sheet-sync
```

程序读取本地 `.env` 和 `config/`，无需启动Webhook服务。

**1. 完全重新搬运并填充**

```bash
uv run stocking-sheet-sync rerun --record-id rec记录ID
```

每次创建新批次，执行备份、按配置填充、交付，**发送通知**。

**2. 指定表格原地填充**

```bash
# 只填历史
uv run stocking-sheet-sync fill --url "表格链接" --history

# 只填预测
uv run stocking-sheet-sync fill --url "表格链接" --forecast

# 历史和预测都填
uv run stocking-sheet-sync fill --url "表格链接" --history --forecast
```

直接修改链接中的线上表格，**不复制、不发送通知**。

**可选参数**

| 参数 | 说明 |
|---|---|
| `--platform pdd` | 指定平台，可重复使用；默认全部 |
| `--overwrite` | 覆盖所选历史、预测已有值；默认保留已有值并补空白 |
| `--as-of 2026-09-23` | 指定基准日；默认上海当天 |
| `--sheet-id 工作表ID` | 指定页签；默认从链接读取或自动识别 |

平台ID：

| 平台 | ID |
|---|---|
| 京东自营 | `jd_self` |
| 京东POP | `jd_pop` |
| 拼多多 | `pdd` |
| 唯品会 | `vip` |
| 天猫超市 | `tmall_supermarket` |

例如，按指定日期强制重填拼多多、唯品会：

```bash
uv run stocking-sheet-sync fill \
  --url "表格链接" \
  --history --forecast \
  --platform pdd --platform vip \
  --as-of 2026-09-23 \
  --overwrite
```

**注意**

- 只处理市场部，原公式和人工需求列保留。
- `fill` 自动按款号、平台判断首次填充或补充，无需模式、行号参数。
- 请求的数据已完整时保留并跳过；缺少历史时核对已填值并补空白，历史完整但预估为空时核对历史后计算。
- 历史不一致只跳过该款该平台，其他款正常补充。已有预测和人工需求保留，数字0算已填。
- 同款新增SKU时，计算使用表内该款全部SKU，只写空白；`--overwrite` 可重填所选历史和预测。
- 补充旧表时使用原基准日 `--as-of`，保持新旧数量周期一致。日志逐款逐平台显示首次填充、补历史、补预估、已完整跳过或冲突原因。
- 去年后续周期来源暂不可用时，表内该周期的日期表头和全部SKU数量均匹配才可参与预测；近30天及去年同期近30天来源不可用时，该平台跳过预测。已有不同预测保留原值。
- 数据源明确返回0时可以填0。
- 新品只支持历史填充，预测暂不支持。
- 基准日9月23日对应近30天：8月24日至9月22日。
- 本地需要能连接数仓、京东自营源 MySQL、Redis 和飞书；目标文件夹、通知接收人按本地配置执行。

查看完整参数：

```bash
uv run stocking-sheet-sync rerun --help
uv run stocking-sheet-sync fill --help
```

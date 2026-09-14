# 历史发货数据读取与表格匹配

检查命令读取飞书表格与数仓，输出本地商品映射、日期覆盖和销量核对结果。检查命令只读；填充命令支持执行销量写入。两者均独立于 Webhook 搬运服务。

## 来源与统计口径

所有平台使用预估日前 30 个完整自然日。例如，预估日为 2026-09-05，则统计 2026-08-06 至 2026-09-04。

| 平台 | 多维表视图对应数仓表 | 实际读取来源 | 限定条件 |
| --- | --- | --- | --- |
| 京东自营 | `ads_rpa_jd_inventory_product_detail` | `bwd_rpa_jd_inventory_product_detail` | `shop_name = kocotree京东自营旗舰店`，以统计截止日的 `outbound_30d` 读取 30 日出库件数 |
| 京东 POP | `ads_whs_outstock_scbqt_base_sku_window` | `dwd_whs_outstock_detail_f` | `dept = 市场部其他`，`shop_id = 10537352`，店铺为 KK-京东--配饰 |
| 拼多多 | `ads_whs_outstock_pdd_base_sku_window` | `dwd_whs_outstock_detail_f` | `dept = 拼多多`，包含该分组店铺 |
| 唯品会 | `ads_whs_outstock_wp_base_sku_window` | `dwd_whs_outstock_detail_f` | `dept = 唯品` |
| 天猫超市 | `ads_whs_outstock_scbqt_base_sku_window` | `dwd_whs_outstock_detail_f` | `dept = 市场部其他`，`shop_id = 11255444`，店铺为天猫超市1-配饰 |

多维表中的视图名带有 `MySQL ` 前缀，部分还带有连接器名称后缀数字。实际数据库表名以数仓元数据为准，保存在 `config/sales-sources.toml` 中。

京东自营的 ADS 表提供当前汇总，BWD 表保留历史日快照，可按指定截止日期读取。出库件数字段与成交件数字段是不同口径。本项目的 `SalesReader` 独立管理 PyMySQL 连接、参数化查询、出库统计和异常处理，连接参数使用 `WAREHOUSE_*`。

其他四个平台按 `outstock_time >= 开始日零点 AND outstock_time < 预估日零点` 查询，要求 `outstock_type = 销售出库`、`outstock_status = 已出库`、`document_status = 已出库`。日期直接采用数仓保存的业务时间，不对无时区字段做 UTC 平移。

## 商品身份与重复处理

表格“商品编码”对应数仓 `spec_code`，京东自营明细对应 `barcode`。款式编码对应 `product_code`。主数据来自 `std_api_jstbz_product_spec_info_f`，按商品编码精确匹配后，再核对款式、名称和颜色规格。

文本编码保留前导零。科学计数文本、存在精度风险的数值编码、重复商品行、身份冲突及缺失字段均进入待核对结果。名称与规格允许全半角和空白差异，但不执行模糊匹配。

商品主数据按完整身份字段做 `DISTINCT`。同一编码对应不同款式、名称或规格时，保留冲突供人工核对。

发货明细以配置的业务唯一键分组；默认组合为数据来源、公司、出库主单号、出库子单号及 SKU。同一业务键下，数量和发货时间一致的重复投影计一次，并报告去重行数。数量、时间冲突或业务键缺失时，相关 SKU 不提供可填数量。件数必须是非负整数，空值不转换为零。

## 日期覆盖与零销量

- 京东自营必须有指定截止日的快照和该 SKU 数据；读取不到时标记缺失，不使用最新日期替代。
- 明细来源逐日检查平台在统计窗口内是否存在已出库记录；缺少日期时，保留已观察销量并阻止作为完整 30 日数量使用。
- 主数据匹配成功、平台 30 日均有数据且目标 SKU 没有发货记录时，记录为“观察窗口内无发货”，数量为零。
- 逐日有记录是覆盖证据，不能证明数仓 ETL 已完整处理当天所有订单。正式自动填充前，应接入数仓任务完成标记或确定业务允许的更新延迟。
- 历史明细采用读取时的当前订单状态，未提供原填表时点的状态快照；与历史人工填写值的差异需要保留核对。

## 行列映射

读取全部行列、公式和合并范围，根据配置的表头行数与别名识别列。市场部列必须位于“市场部”的表头分组下。

分别定位每个平台的历史销量列和需求列。缺列、重复表头、列重叠会被标记或拒绝。只有“市场部其它”的模板无法分别对应 POP 和猫超，需要后续表格结构步骤处理。

目标单元格含公式、数值零或其他已有内容时均标记为非空。空白分隔行之后的 SKU 继续读取，合计行不作为商品行。线上读取期间工作簿版本变化时，整次读取失败并要求重新执行。

## 命令

在项目根目录运行，先安装 Python 依赖，并准备已登录用户身份的 `lark-cli`。数仓凭证放在本项目的 `.env` 或部署环境变量中。默认仅加载当前目录的 `.env`，环境变量优先；可通过 `--db-env-file` 显式指定其他独立凭证文件。

必填参数为 `WAREHOUSE_HOST`、`WAREHOUSE_DATABASE`、`WAREHOUSE_USER`、`WAREHOUSE_PASSWORD`。可选参数包括 `WAREHOUSE_PORT`、`WAREHOUSE_CONNECT_TIMEOUT`、`WAREHOUSE_READ_TIMEOUT`，完整模板见 `.env.example`。

```bash
uv sync
uv run stocking-sheet-sync-inspect \
  --spreadsheet-token '<电子表格 token>' \
  --sheet-id '<工作表 ID>' \
  --as-of 2026-09-05 \
  --output artifacts/check
```

`--source-config` 可指定另一份平台配置。复用本地表格快照时，用 `--snapshot artifacts/check/sheet-snapshot.json` 替换 `--spreadsheet-token` 和 `--sheet-id`，此模式不需要调用飞书 CLI。

输出文件：

- `sheet-snapshot.json`：读取时的完整表格值、公式、合并区域和版本。
- `inspection.json`：商品映射、列映射、数仓来源、日期覆盖和逐项检查结果。
- `matching.csv`：每行对应“商品 × 平台”，包含单元格坐标、观察销量、候选数量、原值及异常。

`observed_quantity` 为读取到的观察数量；`candidate_quantity` 仅在身份、覆盖、目标列及目标空白检查全部通过时提供。`ready` 只表示这些检查通过，命令不执行写入。

命令退出码 0 表示检查完成，业务异常通过报告的 `needs_review` 和 `issues` 表达。配置或整体读取失败返回 1。平台读取失败会在报告中单独列明，避免掩盖其他平台结果。

Webhook 搬运不依赖数仓连接配置。线上表格读取命令需要本机安装并登录 `lark-cli`；当前 Docker 镜像未预装该工具，镜像内可使用本地快照模式。

## 实际填充近30天销量

`stocking-sheet-sync-sales-fill` 在表头结构准备完成后，读取最新工作表和数仓数据，填写市场部各平台的近30天销量。该命令独立于 Webhook，使用与检查命令相同的来源配置和凭证。运行环境需要 `lark-cli` 用户登录。

预览命令：

```bash
uv run stocking-sheet-sync-sales-fill \
  --spreadsheet-token '<电子表格 token>' \
  --sheet-id '<工作表 ID>' \
  --as-of 2026-09-12 \
  --output artifacts/sales-preview
```

预估日必须显式指定。示例窗口为 2026-08-13 至 2026-09-11；窗口不随数仓最新日期自动移动。确认 `request.json` 中的日期、目标单元格、数量和平台合计，读取 `before.json` 中的版本，再执行：

```bash
uv run stocking-sheet-sync-sales-fill \
  --spreadsheet-token '<电子表格 token>' \
  --sheet-id '<工作表 ID>' \
  --as-of 2026-09-12 \
  --apply --expected-revision '<预览读取的版本号>' \
  --output artifacts/sales-applied
```

写入范围为商品行对应的销量单元格，数量以整数类型填写，显示格式为 `0`，字体、对齐、边框等原样式保留。空白分隔行、合计行、需求列及其他部门不参与写入；表内原公式可因输入变化自动重算，公式文本保持一致。

逐项状态与整批行为：

- `write`：身份和来源检查通过，目标为空，可以填写。
- `unchanged`：身份和来源检查通过，目标为相同数值，跳过该单元格。
- `needs_review`：来源日期缺口、商品匹配异常、明细冲突、目标已有不同值或公式等，需要核对。只要一项异常，整批不写入。

同日期重复执行会重新读取数仓核对。全部数量相同则不提交写入请求，返回 `unchanged`；来源数据被修订、已有值与本次结果不同时保留原值并列出冲突，不提供强制覆盖选项。

命令提交前再次核对版本，并使用单次批量请求和非空保护。请在表格无人同时编辑时运行；版本预检不构成并发写入锁。请求不自动重试，网络或回读异常时应先检查实际表格与本地证据。

每次执行建议使用独立输出目录，包含以下文件：

- `inspection.json`、`matching.csv`：商品、来源及原始候选检查结果。
- `before.json`、`sheet-snapshot.json`：写入前完整表格快照，含样式与布局。
- `request.json`：填充日期、逐项状态、候选数值、平台合计及精确写入范围。
- `dry-run.json`：飞书请求预检结果。
- `response.json`、`after.json`：真实提交结果和全量回读快照。
- `result.json`：执行状态、核验数量、平台合计及原公式自动重算记录。
- `error.json`：运行异常；应结合本次请求和回读结果判断是否已产生写入。

全量回读逐格校验：目标销量数值及类型正确、平台合计一致、原内容和样式保留、原公式文本不变、布局不变。返回码 `0` 表示预览、无需写入或写入核验通过；`1` 表示运行异常；`2` 表示存在待核对项且未执行写入。

# China Stock Engine · A 股日频研究仓

WindPy 采集沪深北日线，Parquet 保存日期分区，DuckDB 在本机查询。盘后看市场宽度、行业扩散和量价候选，盘前结合已导入的公告和交易状态复核持仓与自选。Git 管代码和测试，数据与研究记录默认放在本机 `.warehouse/`。

## 安装与查看

在仓库目录运行。Windows PowerShell 用 `py -3.12 -m venv .venv` 建环境，然后把解释器路径替换为 `.\.venv\Scripts\python.exe`，Wind SDK 用 Windows x64 的官方版。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m china_stock_engine status
.venv/bin/python -m china_stock_engine report
.venv/bin/python -m china_stock_engine history 600000.SH --sessions 300
```

也可直接运行安装生成的 `stock-engine`。查本地数据不需要 Wind 登录。换数据目录时用 `STOCK_WAREHOUSE`，或将 `--warehouse PATH` 放在子命令前。

## 一次性初始化

空仓可以采最近一个已完成交易日。有已验证的 V1 Wind 档案时，可以一次导入已有历史：

```sh
.venv/bin/python -m china_stock_engine seed-legacy /path/to/old-repo/data
.venv/bin/python -m china_stock_engine validate
```

可加 `--industry-snapshot DIR` 导入旧仓 `data/reference/industry/snapshot_date=2026-10-16`。报告展示分类观察日和覆盖，超过 45 个自然日便停止使用标签。旧行情按逐日哈希、股票池和已验证交易日历核对。旧窗口早期日期沿用档案的期末股票池，其中的价格可用于当前滚动指标，不能把这些日期当成已重建当日历史股票池。可见时间来自旧档案证据。

## 每日取数

用 `scripts/configure_wind.py` 注册官方 Wind SDK 中真正含 `WindPy.py` 的目录。SDK、账号和凭据留在本机。

```sh
.venv/bin/python scripts/configure_wind.py
.venv/bin/python -m china_stock_engine doctor
.venv/bin/python -m china_stock_engine wind-canary --exclusive-window
.venv/bin/python -m china_stock_engine collect --exclusive-window
.venv/bin/python -m china_stock_engine report --output .warehouse/reports/current.md
```

`--exclusive-window` 表示已确认本机 Wind 登录，并预留了同一账号的独占采集窗口。Trading Desk 的 8765 端口有监听或无法确认时，命令会在加载 WindPy 前退出。新机器先实测沪深北样本的代码、日期、量额单位和矩阵一致性。Mac 与 Windows 各自做原生验证。

采集用 Wind SSE 参考交易日历，默认上海 18:00 后把当天纳入计划，质量达标才发布。每次默认至多补五个已完成但缺失的交易日，已成功日期不会再次取数。可用 `collect --from DATE --through DATE --max-sessions N --exclusive-window` 调整范围和上限。用 `collect --date DATE --refresh --exclusive-window` 显式核查某日修订。没有成交和没有收到行情分别记录，停牌用单独的已证实来源。

日线保存原价和 Wind 报送的当日涨跌幅。涨跌幅从百分数统一为小数，20 日收益按连续交易日复合。当前方案不构造绝对复权 OHLC，也不每天扫描历史修订。已知修订显式刷新受影响日期，保留旧值和观察新值的时间。

## 持仓、自选和公告

`.warehouse/watchlist.csv` 的字段为 `code,kind,label`，例如 `600000.SH,watch,观察备注`。分组用 `holding` 或 `watch`，`label` 可空。可通过 `report --watchlist FILE` 指定其他文件。

| 命令 | 输入 CSV |
| --- | --- |
| `import-industry FILE --as-of-date DATE --source SOURCE` | `code,industry`，同批使用一个行业口径，默认 SW1。 |
| `import-events FILE --source SOURCE` | `event_id,code,category,title,url,published_at,effective_date`。原文使用 HTTPS。最后两列可空，发布时间若有须含时区。 |
| `import-tradability FILE --date DATE --source SOURCE` | `code,suspended,risk_warning,limit_applies,limit_up,limit_down`。布尔为 true/false、1/0 或未知空值。明确存在涨跌幅限制时需一对有效限价。 |

公告类别包括 `earnings`、`earnings_revision`、`dividend`、`share_reduction`、`unlock`、`suspension`、`resumption`、`risk_warning`、`regulatory`、`delisting`、`other`。日线走 WindPy。行业、公告和官方交易状态使用有权访问的 Wind 或交易所/巨潮导出文件。报告只显示已导入事实，缺资料会明确标出。

盘前跑 `report --risk-date YYYY-MM-DD` 可查看指定次日的已导入交易状态，收盘行情继续展示真实日期。用 `report --date DATE --as-of 2026-11-12T22:30:00+08:00` 按指定信息截点查看。保存 `report --json --output .warehouse/reports/DAY.json` 后，可用 `review FILE CODE watch --reason "记录当时的判断"` 将研究记录和该报告绑定。

## 筛选和存储

市场和行业使用有涨跌幅的成交证券计算上涨占比。第一版候选需有连续窗口，前 20 日平均成交额不低于 5,000 万元、当天放量至少 1.5 倍，当日和 20 日回报为正且 20 日跑赢比较基准。有至少五只可比证券时使用行业等权基准，否则使用全市场等权基准。行业上涨占比不低于 55% 且高于市场时优先展示。已证实的停牌、风险警示、收于当日涨停，以及明显的风险名称、一字价和零成交会排除。候选仍需盘前核对。

每天新增一个行情 Parquet。修订仅为受影响日期追加版本，重跑相同数据保持分区及目录字节不变。`catalog.json` 在验证后原子替换，报告和历史只加载目录引用的可见版本。公告导入只写新增记录或修订。跨机器要共享时，选择有授权的数据目录或存储，保持一个写入端。Git 不承担每日数据分发。

```sh
.venv/bin/python -m unittest discover -s tests
```

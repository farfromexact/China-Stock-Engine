# China Stock Engine

China Stock Engine 是一个面向中国 A 股市场的每日数据仓库骨架。它通过同花顺 iFinD Quant HTTP API 采集、标准化、校验并保存可审计的日频数据，供研究、市场雷达和后续分析使用。

本项目借鉴 [China-Commodities-Engine](https://github.com/farfromexact/China-Commodities-Engine) 的核心原则：数据源日期必须与请求日期一致；失败状态与最后有效快照分离；只有通过结构和质量校验的数据才能提升为正式产物；不得用旧数据伪装当天数据。

## 当前正式范围

- 全 A 股证券池：代码、名称、交易所和板块分类。
- 日频行情：不复权开高低收、成交量、成交额和涨跌幅。
- 市场宽度：上涨、下跌、平盘家数，横截面均值/中位数和总成交额。
- 数据质量：证券池规模、行情覆盖率、源日期、重复代码、OHLC 关系、负成交量/成交额和模式签名。

默认提升门槛为证券池不少于 5,000 只、日行情覆盖率不低于 98%，且上交所、深交所和北交所均有覆盖。

财务报表、公告、公司行动、复权因子、指数和高频数据尚未进入正式产物。它们需要独立的指标/报表权限 canary 和 point-in-time 数据契约，不能因为登录成功就假定可用。

## 正式产物

```text
data/
├── last_run_status.json
├── latest/
│   ├── manifest.json
│   ├── universe.parquet
│   ├── daily_quotes.parquet
│   └── market_summary.json
├── market_history.json
└── snapshots/
    └── YYYY-MM-DD/
        ├── manifest.json
        ├── universe.parquet
        ├── daily_quotes.parquet
        └── market_summary.json
```

`data/latest/` 永远指向最近一个已验证交易日。`data/last_run_status.json` 描述最近一次采集尝试，即使失败也不会覆盖最后有效快照。完整日级快照默认保留最近 60 个交易日，紧凑市场历史保留最近 252 个交易日。

## 认证安全

正式自动化使用 iFinD `refresh_token` 换取短期 `access_token`。两者只存在于运行进程内存中，不会写入仓库或日志。

本地只读探针使用隐藏输入：

```powershell
python -m china_stock_engine.cli probe --date 2026-08-18 --prompt-token
```

本地运行一次全市场采集：

```powershell
python -m china_stock_engine.cli run --date 2026-08-18 --prompt-token
python -m china_stock_engine.cli validate
```

不要把 token 放进命令行参数、源代码、`.env.example` 或日志。GitHub Actions 应创建名为 `IFIND_REFRESH_TOKEN` 的加密 Secret。

## 安装与测试

项目要求 Python 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
```

## 每日自动化

GitHub Actions 在工作日北京时间 18:30（UTC 10:30）运行测试、采集和校验。核心采集失败时只提交 `last_run_status.json`；采集与校验均成功时才提交正式数据。节假日仍可能触发调度，但空返回不会伪装成交易日。

## 数据许可

代码仓库与 iFinD 商业数据的再分发权是两件事。默认应保持仓库私有；在改为公开仓库或对外提供数据前，必须确认账号合同允许相应字段、历史范围和再分发方式。详见 [DATA_POLICY.md](DATA_POLICY.md)。

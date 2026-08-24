# China Stock Engine

China Stock Engine 是一个面向中国 A 股的可审计数据引擎。它通过同花顺 iFinD Quant HTTP API 采集、标准化、校验并保存日频事实数据，供本地分析、模型研究和其他系统直接消费。

项目只输出数据和参考信息：

- 不生成股票候选池、综合评分或市场观点；
- 不输出交易建议或收益评价；
- 不以旧数据冒充当天数据，也不为缺失字段制造替代值；
- 每个派生字段都能追溯到源日期、PIT 截止时间和快照哈希。

本项目借鉴 [China-Commodities-Engine](https://github.com/farfromexact/China-Commodities-Engine) 的发布原则：采集尝试与最后有效快照分离，只有通过结构和质量门的数据才能提升到 `latest`。

> 本仓库公开代码，但不公开分发 iFinD 商业数据。`data/` 下的生成物默认由 Git 忽略；使用者需要自备有权使用的 iFinD 账号或 refresh token。

## 数据范围

当前事实层与派生层包括：

- PIT 全 A 股证券池、名称、交易所、板块、上市日、总股本和流通 A 股；
- 未复权日线 OHLC、前收、均价、成交量、成交额、换手率和涨跌幅；
- 上交所交易日历和逐证券行情观测状态；
- 上涨、下跌、平盘家数，横截面均值/中位数和总成交额；
- 1/3/5/20/60 日复权收益、20/60 日历史波动率、滚动成交异常和价格位置；
- 市值、滚动高点距离、相对指数及 PIT 申万行业收益；
- 公司行为、行业、指数成分和可交易性参考的 PIT 输入契约；
- 覆盖率、质量门、模块可用性、内容哈希与数据目录。

默认提升门槛为证券池不少于 5,000 只，日行情和证券主数据覆盖率不低于 98%，扩展字段覆盖率不低于 95%，且沪深北交易所均有覆盖。

历史、复权、行业、指数成分和可交易性模块必须分别通过小范围权限 canary。没有权限时记录 `not_entitled`；字段未知时保持为空。登录成功不等于数据权限可用。

完整字段定义见 [数据字典](docs/DATA_DICTIONARY.md)，存储和许可边界见 [DATA_POLICY.md](DATA_POLICY.md)。

## 数据布局

```text
data/
├── last_run_status.json
├── facts/
│   ├── market/trade_date=YYYY-MM-DD/
│   ├── reference/as_of_date=YYYY-MM-DD/
│   ├── adjustment/as_of_date=YYYY-MM-DD/
│   ├── classification/as_of_date=YYYY-MM-DD/
│   ├── index/trade_date=YYYY-MM-DD/
│   └── tradability/as_of_date=YYYY-MM-DD/
├── features/
│   └── stock_state/trade_date=YYYY-MM-DD/stock_state.parquet
├── latest/
│   ├── manifest.json
│   ├── last_attempt_status.json
│   ├── data_reference_latest.json
│   ├── stock_state.parquet
│   ├── universe.parquet
│   ├── security_reference.parquet
│   ├── daily_quotes.parquet
│   ├── trading_calendar.parquet
│   ├── daily_security_status.parquet
│   └── market_summary.json
├── market_history.json
└── snapshots/
    └── YYYY-MM-DD/
```

`facts/` 是不会随快照保留策略删除的追加式分区；`features/stock_state/` 保存逐股 PIT 派生数据。`latest/` 始终指向最后一个通过质量门的交易日。

`data/last_run_status.json` 描述最近一次采集尝试。鉴权、权限、额度、质量或网络失败不会移动 `latest`。非交易日记录为客观的 `market_closed` 采集状态，不创建空快照。

`data_reference_latest.json` 小于 2 MB，只包含：

- 源交易日、数据截止时间和源快照哈希；
- 质量门结果、字段覆盖率和模块状态；
- 市场宽度、成交额、行情观测和行业事实汇总；
- 数据集名称、行数、哈希和钻取路径。

全市场明细保存在 Parquet，不塞入紧凑 JSON。

## PIT 输入契约

可选模块读取下列规范化文件；缺文件时模块保持 `missing`，缺必需列时直接报错：

- `adjustment_factors.parquet`：`trade_date, thscode, adj_factor, known_at`；
- `corporate_actions.parquet`：`thscode, event_type, published_at, effective_at, known_at`；
- `industry_membership.parquet`：`thscode, classification_system, level, industry_code, industry_name, effective_from, effective_to, known_at`；
- `index_membership.parquet`：`index_code, index_name, thscode, weight, effective_from, effective_to, known_at`；
- `provider_tradability.parquet`：`as_of_date, thscode, is_st, is_suspended, daily_price_limit_pct, lot_size, known_at`；
- `facts/index/trade_date=.../index_quotes.parquet`：至少包含 `trade_date, thscode, open, close`。

所有 PIT 记录必须满足 `known_at <= data_cutoff_time`。有效期数据同时按 `effective_from/effective_to` 过滤；今天的行业、成分或后来修订的数据不会自动回填为历史事实。

## 本地使用

项目要求 Python 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
```

运行隐藏输入的单股只读探针：

```powershell
python -m china_stock_engine.cli probe --date 2026-08-20 --prompt-token
```

分别验证模块权限和返回结构：

```powershell
python -m china_stock_engine.cli canary --module history --date 2026-08-20 --record-status --prompt-token
python -m china_stock_engine.cli canary --module adjustment --date 2026-08-20 --record-status --prompt-token
python -m china_stock_engine.cli canary --module industry --date 2026-08-20 --spec config/ifind_canary_spec.example.json --record-status --prompt-token
```

采集并验证单日快照：

```powershell
python -m china_stock_engine.cli run --date 2026-08-20 --prompt-token
python -m china_stock_engine.cli validate
```

首次回填最近 252 个交易日，并生成逐股状态和紧凑数据参考：

```powershell
python -m china_stock_engine.cli backfill --sessions 252 --end 2026-08-20 --with-adjustment-snapshot --prompt-token
python -m china_stock_engine.cli build-state
python -m china_stock_engine.cli build-report
```

回填先从上交所日历解析准确交易日，再逐日取得 PIT 股票池和证券主数据，并将区间行情按日期窗口与证券批次拆分。它不会用当前股票池回填历史。
回填预取阶段最多使用两路并发，并在认证完成后开始请求；实际并发和批次仍受 iFinD 账户额度与服务端限速约束。

## 凭证安全

自动化使用 `IFIND_REFRESH_TOKEN` 换取短期 access token。两者只存在于进程内存，不写入仓库、数据产物或日志。

不要把 token 放入命令行参数、源代码、`.env.example` 或日志。GitHub Actions 中使用加密 Secret `IFIND_REFRESH_TOKEN`。认证、权限和质量错误不会被伪装成成功。

## GitHub Actions 与本仓数据留存

工作流在工作日北京时间 18:30 运行测试、采集和校验。它不生成或上传 HTML artifact；成功后的完整标准化数据直接提交到本仓的 `data/` 目录和 `main` 分支，供其他 automation 直接 clone 或读取。`concurrency` 防止两个发布任务同时更新指针。

在调用 iFinD 前，工作流会对本仓执行 `git push --dry-run` 并检查仓库容量；没有写入权限或仓库达到 900 MB 容量门槛时会直接失败，不会开始提取。自动化只需要配置加密 Secret `IFIND_REFRESH_TOKEN`；workflow 已请求本仓 `contents: write` 权限用于提交数据。

工作流会恢复历史、补齐缺失交易日、分层验证，并在全部成功后一次性提交完整 `data/`。失败和休市只更新脱敏尝试状态，不移动最后有效快照。

仓库接近 1 GB 前应将长期事实层迁移到对象存储，Git 只保留紧凑状态和参考接口。

## 数据许可

代码公开与 iFinD 商业数据再分发是两件事。启用任何真实云端同步前，必须以账户合同及厂商书面授权为准。详见 [DATA_POLICY.md](DATA_POLICY.md)。

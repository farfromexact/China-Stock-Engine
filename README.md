# China Stock Engine

> 一个每天自动更新、可被程序直接读取的中国 A 股日频数据仓。

China Stock Engine 同时是代码仓和数据仓：它在工作日收盘后通过同花顺 iFinD Quant HTTP API 提取全 A 股日频数据，完成标准化和质量校验后，将结果直接提交到本仓库 `main` 分支的 [`data/`](data/) 目录。

下游 automation、研究脚本或服务不需要等待 HTML 报告或临时 artifact；直接读取此仓库中的 Parquet 和 JSON 文件即可。换言之，这个仓库的目标是提供一份有版本、有日期、有质量状态的 A 股事实数据源，而不是提供选股、交易信号或市场观点。

## 仓库用途

这个仓库负责四件事：

1. 每个工作日北京时间 18:30 自动提取当日 A 股数据；
2. 校验证券池、行情、参考数据和交易日历的覆盖率与结构；
3. 将通过校验的数据持久化到本仓库，而非只保留在运行日志或 artifact；
4. 为其他 automation 提供稳定、可追溯的读取入口。

项目只输出数据和参考信息：

- 不生成主观候选池、综合评分或市场观点；只提供可复算的确定性排序筛选；
- 不输出交易建议或收益评价；
- 不以旧数据冒充当天数据，也不为缺失字段制造替代值；
- 每个派生字段都能追溯到源日期、PIT 截止时间和快照哈希。

本项目借鉴 [China-Commodities-Engine](https://github.com/farfromexact/China-Commodities-Engine) 的发布原则：采集尝试与最后有效快照分离，只有通过结构和质量门的数据才能提升到 `latest`。

> `data/` 是本仓库的实际数据输出，提交在 Git 历史中供下游自动化读取；仓库绝不提交 iFinD 原始响应、token 或其他凭证。公开分发和下游使用仍须符合 iFinD 账户合同与数据许可。

## 给其他 automation 的读取入口

使用者应从 [`data/latest/`](data/latest/) 读取当前最后一个已验证快照，并先检查 [`data/latest/manifest.json`](data/latest/manifest.json) 中的 `trade_date`、`verified` 和质量指标。不要仅依据文件存在就把数据认定为当天数据。

- `data/latest/daily_quotes.parquet`：当日逐证券未复权行情；
- `data/latest/security_reference.parquet`：证券名称、交易所、板块、上市日和股本等 PIT 参考数据；
- `data/latest/stock_state.parquet`：逐证券 PIT 派生状态；
- `data/latest/market_summary.json`：市场宽度和成交汇总；
- `data/latest/opportunity_inputs_latest.json`：供 ChatGPT/Automation 读取的紧凑事实摘要与确定性 screens；
- `data/snapshots/YYYY-MM-DD/`：按交易日冻结的完整验证快照；
- `data/last_run_status.json`：最近一次采集尝试的状态，失败不会覆盖 `latest`。

每次成功发布都会生成一个普通 Git commit，因此下游可固定某个 commit SHA 复现一次读取，也可始终读取 `main` 上的 `data/latest/` 获取最新有效数据。

## 数据范围

当前事实层与派生层包括：

- PIT 全 A 股证券池、名称、交易所、板块、上市日、总股本和流通 A 股；
- 未复权日线 OHLC、前收、均价、成交量、成交额、换手率和涨跌幅；
- 上交所交易日历和逐证券行情观测状态；
- 上涨、下跌、平盘家数，横截面均值/中位数和总成交额；
- 1/3/5/20 日原始涨跌幅复合收益，以及数据充分时的复权收益；
- 20 日历史波动率、滚动成交异常、价格位置、市值和 20 日高点距离；
- 公司行为、行业、指数成分和可交易性参考的 PIT 输入契约；
- 覆盖率、质量门、模块可用性、内容哈希与数据目录。

默认历史目标为 20 个交易日。`1D/3D/5D/20D` 分别报告 readiness 和覆盖率；历史不足 20 日时仍计算已经合法可得的短周期字段，不会让整张 `stock_state` 失效。60 日和 252 日字段暂时保持 `null`，并在 readiness 中列为 unavailable。

默认提升门槛为证券池不少于 5,000 只，日行情和证券主数据覆盖率不低于 98%，扩展字段覆盖率不低于 95%，且沪深北交易所均有覆盖。质量门同时与上一交易日比较股票池、行情/参考覆盖率、交易所和板块数量、无行情观测、总成交额及前收连续性；异常会以 warning 或 error 明确记录。

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
│   ├── opportunity_inputs_latest.json
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

`opportunity_inputs_latest.json` 同样控制在 2 MB 内，包含：

- `trade_date / generated_at / source_snapshot_sha256` 与完整 PIT 时间；
- 数据模式、各周期 readiness 和关键字段覆盖率；
- 市场宽度、成交额、极端涨跌数量及 1D/3D/5D/20D 横截面变化；
- 板块和确定性市值分桶汇总；
- 单日涨跌、成交额、成交/换手扩张、价格×成交×收盘位置、跨周期动量、gap/日内结构和 3D/5D/20D 相对强度 screens；
- 按 SSE Main、SZSE Main、STAR、ChiNext、BSE 以及五档市值分桶分别计算的中性化绝对涨跌 screens；
- `return_1d/3d/5d/20d_pctile`、`amount_change_pctile`、`turnover_change_pctile`、`close_location_pctile`、成交额/换手排名及 `amount / float_market_cap`；
- 最多 150 只证券的 `candidate_union`：仅对全部 screen 的 Top 25 结果去重，保留触发 screen、screen 内 rank/percentile、原始事实与确定性 `contradiction_flags`。

每个独立 screen 最多 25 行。`candidate_union.screen_count` 只是同一证券被多少个确定性过滤器捕获，不是股票评分；所有输出均不包含综合分数、买卖标签、推荐或收益评价。该 JSON 使用键排序、一层缩进和紧凑分隔符的确定性多行序列化，兼顾 GitHub connector 分段读取与仓库体积。

## PIT 输入契约

可选模块读取下列规范化文件；缺文件时模块保持 `missing`，缺必需列时直接报错：

- `adjustment_factors.parquet`：`trade_date, thscode, adj_factor, known_at`；
- `corporate_actions.parquet`：`thscode, event_type, published_at, effective_at, known_at`；
- `industry_membership.parquet`：`thscode, classification_system, level, industry_code, industry_name, effective_from, effective_to, known_at`；
- `index_membership.parquet`：`index_code, index_name, thscode, weight, effective_from, effective_to, known_at`；
- `provider_tradability.parquet`：`as_of_date, thscode, is_st, is_suspended, daily_price_limit_pct, lot_size, known_at`；
- `facts/index/trade_date=.../index_quotes.parquet`：至少包含 `trade_date, thscode, open, close`。

manifest、`stock_state` 和两个紧凑 JSON 均明确记录 `collection_started_at`、`collection_completed_at`、`configured_decision_cutoff` 与 `effective_pit_cutoff`。其中 `effective_pit_cutoff = min(configured_decision_cutoff, collection_completed_at)`，绝不会晚于实际采集完成时间。所有 PIT 记录必须满足 `known_at <= effective_pit_cutoff`。有效期数据同时按 `effective_from/effective_to` 过滤；今天的行业、成分或后来修订的数据不会自动回填为历史事实。

关键 `manifest.json` 不存在时可视为 missing；JSON 损坏、不是对象或 schema 不兼容会立即报错，不会静默当成空对象继续运行。

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

首次回填最近 20 个交易日，并生成逐股状态和两个紧凑数据接口：

```powershell
python -m china_stock_engine.cli backfill --sessions 20 --end 2026-08-20 --with-adjustment-snapshot --prompt-token
python -m china_stock_engine.cli build-state
python -m china_stock_engine.cli build-report
```

回填先从上交所日历解析准确交易日，再逐日取得 PIT 股票池和证券主数据，并将区间行情按日期窗口与证券批次拆分。它不会用当前股票池回填历史。
回填预取阶段最多使用两路并发，并在认证完成后开始请求；实际并发和批次仍受 iFinD 账户额度与服务端限速约束。
已有日期的规范化参考分区和行情分区会先做 schema/日期校验并直接复用；只有缺失日期窗口才调用 iFinD。相同日期重复执行是幂等的。

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

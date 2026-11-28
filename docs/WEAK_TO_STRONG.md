# 弱转强数据仓

盘后扫描 Wind 沪深京全 A 股票池，发布一份供 LLM 阅读的候选包。本机维护 30 个 Wind 交易日的原始开高低收、成交量（股）、成交额（元）、复权因子与真实观测时间。股票池和上市日决定应观测的日期，观测表区分成交、零成交、缺行情与成交量额不完整。

## 规则与阅读方式

触发日为 T。观察 T−6 到 T 的七个连续交易日，七天都要有有效成交。

1. T−6 收盘到 T−1 收盘的复权收益率不高于 −5%。
2. T 的复权收盘严格高于 T−3 至 T−1 的复权最高价，这一价位是观察线。
3. T 的成交额至少为 T−5 至 T−1 成交额中位数的 1.3 倍。

每日查看本交易日和此前两个 Wind 交易日，同一股票取最新触发。在当日触发用 new，之后每个收盘都守在观察线上用 holding。任一已观测收盘跌破即用 failed，并记录首次跌破日。触发之后缺行情或零成交且尚未观察到跌破用 unconfirmed。个股日收益与当天有连续成交且有复权观测的股票中位数比较。

下游读取 main 的 `data/weak_to_strong/latest.json`。包头包含实际交易日 `trade_date`、实际观测时点 `observed_at`、覆盖率、规则版本、输入哈希和三日市场背景。包内最多 24 条新触发或守住的候选及 6 条失效记录。先按触发日排序，再比较同日超出市场中位数的百分点，代码用于并列排序。全量数量和截断数量见 `counts`，本地版本的 `events.json` 存三日全部触发及其证据，包括同一股票重复触发的记录。

`watch_price_at_trigger_basis` 以触发日的价格口径表示。跨日用 `watch_index` 与 `close_index_path` 比较，两者都以弱势窗口第一个收盘为 100，缺观察为 JSON null。两种价位都来自同一复权序列。高开低收指当日开盘高于前收且收盘低于开盘。全天单一价格表示日最高价等于最低价。

给 LLM 的任务可直接写成：

> 用候选包的实际交易日和任务指定交易日核对新鲜度，注明数据截止交易日。至多解释 5 只候选，逐只给出弱势、收复观察线、成交额倍数、相对当天市场的表现和最新状态。引用同一复权口径的观察线说明什么情况会使信号失效。零命中按空结果处理，候选被截取时报告总数和省略数。

## 本机运行

本机审核后的 Wind profile 和单机锁沿用 [Wind Windows 接手说明](WINDOWS_WIND_HANDOFF.md) 与 [本地调度](WIND_LOCAL_DAILY.md)。两种本地入口产品标识为 `weak_to_strong` 和 `legacy_300d`。本地 daily 默认选择弱转强。可在私有配置中设置 product，或在命令中传入相应的 --product。该配置使用现有 `enabled`、`auto_git_push`、`allow_dashboard` 和授权文件，远端调度保持 idle。

```powershell
.\.venv\Scripts\python.exe -m china_stock_engine.wind_weak_to_strong replay --end 2026-11-19
.\.venv\Scripts\python.exe -m china_stock_engine.wind_collect canary --product weak_to_strong --end YYYY-MM-DD --profile .private-stock-data/wind/profile.json --batch-size 5 --dashboard-coexistence
.\.venv\Scripts\python.exe -m china_stock_engine.wind_daily check --product weak_to_strong
.\.venv\Scripts\python.exe -m china_stock_engine.wind_daily tick --product weak_to_strong
.\.venv\Scripts\python.exe -m china_stock_engine.cli validate --product weak_to_strong --workspace .private-stock-data/wind/weak_to_strong
```

按私有配置用 `arm --login-confirmed --dashboard-coexistence` 确认共存窗口，或在独占模式下改用 `--exclusive-window`。单独 canary 只输出核对结果。历史验收可用 `tick --product weak_to_strong --end YYYY-MM-DD`，用独立 workspace 且配置 `auto_git_push=false`。回放使用最新已验证的旧 Wind 数据版本，输出当时版本的实际观测时间。

盘后入口先确定已结束的 Wind 交易日，上海时间 15:30 为当天候选可验证的最早时间。仅从当前已验证 Wind 底表复用，首轮可投影经验证的旧版 Wind 快照。每次有新交易日时做整窗原始和复权收盘探针、20 组之一的全部字段轮换核查，以及新增和缺口的两份原始／复权日线下载。显式 `tick --audit-all` 会核查全池的完整字段。轮换核查分批发现非收盘字段修订。

候选通过沪深京股票池、30 日分交易所覆盖率、复权配对、观测时点、大小和哈希校验后发布。同一次运行的私有事实和证据由 `<workspace>/weak_to_strong/current.json` 指向独立 Wind vintage，保留该产品的最新滚动版本。失败保留最后有效包，下一次优先恢复已验证候选或待推送的 Git 提交。公开发布沿用仓库数据许可和 `auto_git_push` 配置，只提交候选 JSON。主分支运行时可用 `--product legacy_300d` 显式选择旧产品。

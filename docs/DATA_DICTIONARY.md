# China-Stock-Engine 数据字典

本项目只发布可审计的数据与派生字段，不生成候选池、综合评分、市场观点或交易建议。真实 iFinD 数据仅允许写入获得许可的私有存储；公开仓库只包含代码、配置模板与合成测试。

## 分层与粒度

| 层级 | 路径 | 主键 / 粒度 | 说明 |
| --- | --- | --- | --- |
| 市场事实 | `facts/market/trade_date=YYYY-MM-DD/daily_quotes.parquet` | `trade_date + thscode` | 未复权日线 OHLCV、均价、成交额、换手率 |
| 证券状态 | `facts/market/trade_date=YYYY-MM-DD/daily_security_status.parquet` | `trade_date + thscode` | 当日行情观测状态；缺失不推断为停牌 |
| 证券参考 | `facts/reference/as_of_date=YYYY-MM-DD/security_reference.parquet` | `as_of_date + thscode` | 名称、交易所、板块、上市日、总股本和流通 A 股 |
| 股票池 | `facts/reference/as_of_date=YYYY-MM-DD/universe.parquet` | `as_of_date + thscode` | 当日 PIT A 股股票池 |
| 交易日历 | `facts/reference/as_of_date=YYYY-MM-DD/trading_calendar.parquet` | `calendar + trade_date` | 上交所交易日历观测 |
| 复权与公司行为 | `facts/adjustment/as_of_date=YYYY-MM-DD/` | 证券、事件或交易日 | 调整因子及公司行为；保留 `published_at`、`effective_at`、`known_at` |
| 分类 | `facts/classification/as_of_date=YYYY-MM-DD/` | 证券或指数成分的有效期区间 | 申万行业及指数成分；使用 `effective_from/effective_to/known_at` |
| 可交易性参考 | `facts/tradability/as_of_date=YYYY-MM-DD/` | `as_of_date + thscode` | ST、停牌、价格限制、交易单位及可用资格字段 |
| 逐股派生数据 | `features/stock_state/trade_date=YYYY-MM-DD/stock_state.parquet` | `trade_date + thscode` | 仅使用数据截止时间之前已知的事实生成 |
| 紧凑参考接口 | `latest/data_reference_latest.json` | 单个最后有效快照 | 质量、覆盖率、市场事实汇总、数据目录与钻取路径；小于 2 MB |

`latest/` 只指向最后一个通过质量门的快照。采集或权限失败只更新尝试状态，不覆盖最后有效数据。

## `stock_state` 字段组

### 身份与时间

- `schema_version`：逐股状态结构版本。
- `trade_date`：源交易日。
- `data_cutoff_time`：PIT 数据截止时间，默认当日 `20:15 Asia/Shanghai`。
- `thscode`、`security_name`、`exchange`、`board`：证券标识。
- `source_snapshot_sha256`：输入快照的稳定内容哈希。

### 价格与复权

- `raw_close`：未复权收盘价。
- `adj_factor`：截止数据时间已知的调整因子。
- `adjusted_close`、`forward_adj_close`：用于跨期比较的复权价格。
- `total_return_index`：以序列首个有效复权价格归一为 100。
- `corporate_action_flag`、`corporate_action_types`：当日已知且生效的公司行为事实。
- `adjusted_ready`：当前记录是否具备有效复权价格。

### 收益、波动与成交

- `return_1d_pct`、`return_3d_pct`、`return_5d_pct`、`return_20d_pct`、`return_60d_pct`：复权收益率。
- `rv20_pct`、`rv60_pct`：日收益标准差年化后的历史波动率。
- `turnover_z20`、`amount_z20`、`volume_z20`：20 日滚动标准化值。
- `gap_pct`、`intraday_range_pct`、`close_location`、`close_vs_avg_pct`：当日价格位置事实。
- `turnover_change_pct`、`amount_change_pct`、`adt20`：成交变化及 20 日平均成交额。

### 规模、区间位置与相对数据

- `float_market_cap`、`total_market_cap`：以当日未复权收盘价计算的流通和总市值。
- `distance_from_high_20_pct`、`distance_from_high_60_pct`、`drawdown_from_high_252_pct`：相对滚动高点距离。
- `relative_return_industry_20d_pct`：相对当日 PIT 申万一级行业均值的 20 日收益差。
- `relative_return_csi300_20d_pct`、`relative_return_csi1000_20d_pct`：相对指数 20 日收益差。
- `sw1_code/name`、`sw2_code/name`、`index_memberships`：截止时间有效的分类与成分事实。

### 历史与可交易性参考

- `history_sessions`、`history_start_date`、`listing_age_calendar_days`：可用历史长度与上市时长。
- `is_st`、`is_suspended`、`limit_up`、`limit_down`、`one_word_limit`：供应商字段和确定性价格限制标记。
- `daily_price_limit_pct`、`lot_size`、`stock_connect_eligible`、`margin_eligible`、`short_sell_eligible`：交易规则和资格参考字段。
- `tradability_state`：`clear`、`restricted` 或 `unknown`。这是按公开规则生成的数据状态，不是下单或投资判断。
- `tradability_reason_codes`：状态对应的缺失、限制或阈值原因码。

## 空值、PIT 与质量规则

- 未知字段保持 `null`，不得以零、否或停牌替代。
- 行业、指数成分、公司行为和供应商状态必须满足 `known_at <= data_cutoff_time`。
- 有效期数据同时满足 `effective_from <= trade_date <= effective_to`；开放结束日视为仍有效。
- 无权限的模块记录为 `not_entitled`，不制造替代数据。
- 原始价格与复权价格并存；复权因子不足时，相关复权字段保持为空。
- 同一交易日相同输入应产生相同源快照哈希，且不会新增重复分区。

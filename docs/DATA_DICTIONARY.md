# China-Stock-Engine 数据字典

本项目只发布可审计的数据、确定性派生字段与确定性 screens，不生成主观候选池、综合评分、市场观点或交易建议。真实 iFinD 数据的存储与分发必须符合账户合同和数据许可；仓库不保存原始 iFinD payload 或凭证。

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
| 完整确定性输入 | `latest/opportunity_inputs_latest.json` | 单个最后有效快照 | 市场事实、周期 readiness、全部确定性 screen Top 25 和最多 150 只去重候选；小于 2 MB |
| LLM/Automation 接口 | `latest/opportunity_radar_latest.json` | 单个最后有效快照 | 不重复 screen 行的 Top 100 candidate union；目标 220–250 KiB，硬上限 300 KiB |

`latest/` 只指向最后一个通过质量门的快照。采集或权限失败只更新尝试状态，不覆盖最后有效数据。

## `stock_state` 字段组

### 身份与时间

- `schema_version`：逐股状态结构版本。
- `trade_date`：源交易日。
- `collection_started_at`、`collection_completed_at`：实际采集开始与完成时间。
- `configured_decision_cutoff`：配置的当日研究截止时间，默认 `20:15 Asia/Shanghai`。
- `effective_pit_cutoff`：实际 PIT 截止时间，取配置截止与采集完成时间的较早值。
- `data_cutoff_time`：兼容字段，等于 `effective_pit_cutoff`。
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

- `raw_return_1d_pct`、`raw_return_3d_pct`、`raw_return_5d_pct`、`raw_return_20d_pct`：由供应商逐日 `change_ratio` 复合得到的未复权周期变化；分别需要 1/3/5/20 个有效交易日观测。
- `return_1d_pct`、`return_3d_pct`、`return_5d_pct`、`return_20d_pct`：有 PIT 调整因子时计算的复权收益率；调整因子不足时保持 `null`。
- `return_60d_pct`：兼容保留字段，当前版本固定 `null`，readiness 标记 unavailable。
- `rv20_pct`：复权日收益标准差年化后的 20 日历史波动率；历史或调整因子不足时为空。
- `rv60_pct`：兼容保留字段，当前版本固定 `null`。
- `turnover_z20`、`amount_z20`、`volume_z20`：20 日滚动标准化值。
- `gap_pct`、`intraday_range_pct`、`close_location`、`close_vs_avg_pct`：当日价格位置事实。
- `turnover_change_pct`、`amount_change_pct`、`adt20`：成交变化及 20 日平均成交额。

### 规模、区间位置与相对数据

- `float_market_cap`、`total_market_cap`：以当日未复权收盘价计算的流通和总市值。
- `distance_from_high_20_pct`：相对 20 日复权高点距离。
- `distance_from_high_60_pct`、`drawdown_from_high_252_pct`：兼容保留字段，当前版本固定 `null`。
- `relative_return_industry_20d_pct`：证券20D复权收益减逐日PIT申万一级同业等权日收益的复合值；每一天只用当天15:00前已知且有效的成员关系，不把当前分类回填20日。任一天缺少必要成员/收益，结果为空。
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
- `daily_price_limit_pct` 或必要行情缺失时，`limit_up`、`limit_down`、`one_word_limit` 都保持 `null`；unknown 不等于 false。
- 行业、指数成分、公司行为和供应商状态必须满足 `known_at <= effective_pit_cutoff`。
- 有效期数据同时满足 `effective_from <= trade_date <= effective_to`；开放结束日视为仍有效。
- 无权限的模块记录为 `not_entitled`，不制造替代数据。
- 原始价格与复权价格并存；复权因子不足时，相关复权字段保持为空。
- 同一交易日相同输入应产生相同源快照哈希，且不会新增重复分区。
- 历史总体状态为 `ready/partial/missing`，并分别给出 `1D/3D/5D/20D` readiness；部分历史仍可发布合法字段。
- 质量报告对上一交易日检查 universe、行情/参考覆盖率、交易所/板块数量、`no_quote_observed`、总成交额及前收连续性，并保留具体 drift 指标和 alerts。
- `manifest.json` 缺失可视为 missing；损坏、非对象或不支持的 schema 必须 fail closed。

## `opportunity_inputs_latest.json`

根字段包括：

- `schema_version`、`document_type`、`trade_date`、`generated_at`、`source_snapshot_sha256`；
- `pit_timing`、`data_mode`、`readiness`、`field_coverage`；
- `market`、`board_summary`、`market_cap_bucket_summary`、`data_quality_drift`；
- `cross_sectional_features`、`deterministic_screens`、`candidate_union`、`candidate_union_metadata`、`contradiction_flag_definitions` 与 `drilldown`。

`schema_version=4` 的确定性 screens 分为：

- 基础横截面：最大正/负涨跌、最高成交额、成交额/换手扩张、强/弱收盘位置、3D/5D/20D 相对强度；
- 价格×成交确认：上涨/下跌分别与成交额及换手扩张或收缩组合，要求成交额变化与换手变化同向；
- 多周期结构：1D/3D/5D 同向正收益、1D 相对日均化 3D/5D 趋势加速、强 5D 弱 1D、弱 5D 强 1D；
- 涨跌×收盘位置：以 1D 横截面顶部/底部 20% 结合收盘位置阈值；
- gap/日内结构：高开强/弱收盘、低开强收盘、大振幅强/弱收盘；
- 中性化绝对涨跌：在 SSE Main、SZSE Main、STAR、ChiNext、BSE 和五档总市值分桶内分别排名。

每个独立 screen 最多 25 行；每行包含证券事实与 `trigger.metric/value/rank/percentile/rule`。`percentile` 使用当日全市场横截面 average-rank 百分位，数值越大代表原始 metric 越大。

横截面字段包括：

- `return_1d_pctile`、`return_3d_pctile`、`return_5d_pctile`、`return_20d_pctile`；
- `amount_change_pctile`、`turnover_change_pctile`、`close_location_pctile`；
- `amount_rank`、`turnover_rank`：降序名次，1 为当日最高；
- `amount_to_float_market_cap`：当日成交额除以流通市值；分母缺失或不大于零时为 `null`。
- `amount_z20`、`turnover_z20`、`adt20`：仅在合法 20-session 历史齐备时输出；否则保持 `null` 并通过 `field_coverage` 明示覆盖率。

`candidate_union` 是所有screen行的证券去重并集，最多150只。每行保留 `union_order`、`triggered_screens`、`screen_count`、`best_screen_rank`、`screen_ranks`、`screen_percentiles`、`percentiles`、`contradiction_flags` 和 `facts`。按下述 `family_round_robin_v1` 取样，`screen_count` 和最佳名次不再参与跨类别截断；它们仍是事实统计，不是评分。

`relative_strength_3d/5d/20d` 是原始供应商日涨跌幅复合收益减全市场同期横截面中位数，不是行业或指数超额收益；trigger.basis 明示此口径。空screen不自动表示无数据：`state` 表示metric是否可用；`match_state` 区分 `observed_matches`、确定性单指标全覆盖时的 `confirmed_no_matches` 与无法证明所有条件的 `unknown`。

`contradiction_flags` 仅在所需字段已知且规则成立时出现：`price_up_but_weak_close`、`volume_spike_but_negative_return`、`strong_5d_but_negative_1d`、`tiny_absolute_amount`、`micro_cap`、`high_turnover`、`gap_up_failed`。根字段 `contradiction_flag_definitions` 保存精确阈值；未知值不会制造 flag，也不会被当作 `false` 事实。

该文件采用键排序、一层缩进和紧凑分隔符的确定性多行 JSON 序列化，方便 GitHub connector 按行分段读取；实际落盘文件必须小于 2 MB。所有 screen 和 union 均不包含评分、推荐、买卖动作或收益评价。

## `opportunity_radar_latest.json`

这是从同一份 `opportunity_inputs_latest.json` 确定性投影得到的 LLM/Automation 接口。根字段保留交易日、源快照哈希、PIT 时间、readiness、字段覆盖、市场/板块/市值汇总、数据质量漂移、screen catalog、矛盾标记定义和钻取路径，但不重复保存各个 screen 的 Top 25 证券行。

`candidate_union` 固定最多 100 只，截断和 tie-break 顺序严格为：

1. 固定 `selection_policy_version=family_round_robin_v1`，类别名升序轮转，每类每轮取一只未出现证券；
2. 类内对 `{metric,definition,scope}` 的canonical JSON SHA256升序轮转，每条规则每轮取一只；类内同规则别名及相同有序队列不增加槽位，相同规则配置到不同类别直接失败；
3. 规则内部按screen rank、thscode升序，类内和全局分别去重；从全部捕获结果计算序列，不先按重叠数挑150再挑100。前100严格等于前150的前100。

Radar结构版本为2；`selection_diagnostics` 展示截断前后数量、板块、市值桶、正负涨跌和±9.5%数量，以及逐screen的 eligible/captured/selected/dropped。`screen_catalog` 包括零入选screen。价量子类别统一属于TAPE，不提供“独立证据数量”。原schema3输入/schema1 radar历史文件不改写，不能混用旧截断与新policy。

输出使用 `union_order=1..N` 标识上述确定性传输顺序，不使用整体 `rank`，不引入主观权重或综合分数。每只证券保留 screen evidence、screen 内 rank/percentile、evidence family、关键原始事实、横截面 percentile、`contradiction_flags` 和 availability。

availability 对可交易性相关字段使用以下状态，不依赖 Python/JSON truthiness：

- `not_ready`：所需模块 missing、stale 或 not_entitled，且没有证券级观测；
- `unknown`：模块可用或部分可用，但该证券字段未知；
- `confirmed_false` / `confirmed_true`：明确观测到布尔假/真；
- `confirmed_value`：明确观测到非布尔数值；
- `confirmed_clear` / `confirmed_restricted`：明确观测到可交易性 clear/restricted。

`generated_at` 必须等于接口 `pit_timing.collection_completed_at`（行情及合格补充因子的最大实际完成时间），相同完整输入和规则版本的重复构建字节一致。行情manifest保留原始行情完成时间，`feature_pit_timing` 单独记录派生层时间；后采集因子必须仍在当日配置截止时间以内，否则不能倒灌历史决策。`source_snapshot_sha256` 不含派生产物；`feature_input_sha256` 另绑定历史、日历、可选事实、特征时间和参数，避免循环哈希。文件使用确定性多行JSON；220–250 KiB是软目标，300 KiB是硬上限。超限会fail closed，不提升latest，也不自动删除字段或减少候选。

## 20日窗口和研究事实版本

逐股状态和data reference现为schema4。历史默认读取21个价格点，研究窗口仍为20日。每个horizon分别列出 `coverage`（供应商原始复合收益）、`adjusted_coverage`、`adjusted_state`、`adjusted_price_points_required`。`history_sessions` 是读取窗口内实际价格观测数（上限20），不意味着连续性；收益计算另检查连续窗口。`price_points_loaded` 计全市场已读价格日期，`unconfirmed_calendar_dates` 明示旧日历未认证的缺口。

研究事实入口为schema1，详细字段、单位、时间及修订语义见 [RESEARCH_CONTRACT.md](RESEARCH_CONTRACT.md)。批次、研究索引与分片均不保留raw响应；财务或公告缺失不是 `not_entitled`，后者只传播截止时间前已记录的明确权限拒绝。

总市值分桶为固定人民币边界：`<50 亿`、`50–200 亿`、`200–800 亿`、`800–3000 亿`、`>=3000 亿`。分桶只是汇总维度，不代表投资风格判断。

## 真实补充采集

`supplemental_manifest.json` 是独立的schema1采集指针：`market_trade_date` 是行情源日，
`as_of_date` 是实际采集日，`scope_codes` 限定范围；逐模块给出规范化文件 `path/sha256/rows/completed_at`。
它不证明全市场覆盖，也不替代行情manifest。失败只更新独立attempt，保留最后有效补充指针。

财务当前映射 `ths_np_atoopc_pit_stock` 为人民币元、合并YTD归母净利润；
`ths_regular_report_actual_dd_stock` 为实际披露日期，日期精度保守转北京时间日末。
公告 `seq` 映射为事件身份；不从标题推断事件已经完成。可选
`source_url_kind` 区分原文链接与供应商查询文档，`document_access` 明示
`authenticated_link_omitted/public_link_not_downloaded/not_resolved`；鉴权链接的参数不落盘。
没有下载文档时 `document_sha256=null`。

复权新增 `base_date/collection_completed_at/adjustment_method`，使用日线CPS=2分红再投口径。
`adj_factor=forward_adj_close/raw_close`，原价来自已有规范化行情；每次因子序列保留完整vintage，
不同基点不可拼接。它不替代含分红/送转/配股明细的公司行为事实表。

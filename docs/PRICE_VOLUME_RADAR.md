# 价量机会发现：第一版契约

本轮取代基本面优先筛选：仓库对 PIT 全市场逐证券计算固定规则，Automation 只对少量候选核实催化、检查风险和下一交易窗口。财务、公告和商品暴露为可选研究资料，不是技术扫描的前置门槛。

## 历史与参数

`config/technical_signals.json` 是默认参数入口，`build-report --technical-config FILE` 可以覆盖。默认加载最近 300 个交易日；`build_stock_state` 不再截断到 21 点。60 日收益、RV60、60/252 日高点距离按各自完整窗口生成，历史不够保留 null，不妨碍已有 61 日有效 OHLC 的形态计算。

形态输入为同一 PIT 版本的统一复权 OHLC：已有 iFinD 按因子乘法，AKShare 腾讯原生后复权按已验证的乘数/现金偏移映射。不同复权基准不得拼接。突破位按当天的乘数/偏移逆映射到未复权报价；纯乘法因子等价于偏移为零。成交额单位为元；成交量只用于检查是否存在有效交易，不把成交额解释为净资金流入。

普通池要求上市至少 120 个交易日、前 20 日平均成交额至少 5000 万元。上市交易日计数来自已观测的交易日历；只有有限日历时记录下界，无法证明达到门槛则待核。已确认 ST、退市整理、停牌及非交易观测排除；状态未知保留数学命中，标为 pending。名称明确显示 ST 或退市整理也构成正面排除证据，名称正常不等于确认没有风险。

缺行情、缺因子、未验证或截点后才观测的行情，以及零成交/不合法 OHLC，均打断技术窗口，不删除空档后跨日期计算。新采交易所完整交易日查询保存查询区间边界，用来认证区间内休市日；旧单日缓存不能认证整个区间，日历缺口保守保留，不用工作日假装已确认交易日。停牌后需重新积累完整有效窗口，这是第一版保守的数据策略。

## 固定规则

BREAKOUT 的三个条件同时成立：

1. 今日复权收盘价 > **今天之前** 60 个交易日复权最高价的最大值。
2. 今日成交额 / **今天之前** 20 日平均成交额 ≥ 1.5。
3. (今日收盘 − 今日最低) / (今日最高 − 今日最低) ≥ 0.75。

平盘 K 线的收盘位置为未知，不视作强收盘。

WATCH 的三个条件同时成立：

1. 收盘价 > MA20 > MA60。
2. 尚未收盘突破，且 0 ≤ (前 60 日高点 − 收盘价) / ATR20 ≤ 1。
3. ATR5 / ATR20 ≤ 0.70。

WATCH 的均线和 ATR 包含今天。ATR 为真实波幅的**简单平均**；真实波幅是 max(H−L, |H−昨收|, |L−昨收|)，缺昨收时不补造真实波幅。最近 5 日均成交额 / 最近 20 日均成交额 ≤ 0.80 只打缩量标签，不作硬门槛。

突破后 (收盘价 − 突破锚点) / **截至昨日** ATR20 > 1 时保留 BREAKOUT，但 entry_state=wait。涨停、缺少交易制度/状态资料也不等同于可执行。review_next_session 仅表示下个窗口值得复核，绝不代表已成交。

## 产物与历史

- `latest/technical_scan.parquet`：全证券池每只股票的规则、原始/复权价位、排除与待核原因、A0/A1/A2、锚点与生命周期。
- `latest/technical_signals_latest.json`：全部数学命中和数量/原因统计，无 Top100 预筛选。
- `latest/opportunity_inputs_latest.json`：主 candidate_union 为全量技术命中；旧榜单移到 legacy_rankings。
- `latest/opportunity_radar_latest.json`：最多 100 只（参数可减少），先复核下一窗口、再 WATCH、待核、偏离等待；组内按代码排序，不是吸引力评分。提供全量匹配入口和实际展示/省略数量。
- `features/technical_signals/trade_date=DATE/`：日终全量扫描和哈希凭据，随快照清理仍保留。current research 另存 `research_view=HASH/`，不改写历史日终台账。
- `facts/market/trade_date=DATE/market_receipt.json`：持久行情验证凭据；兼容旧快照 manifest，但已经丢失凭据的历史不冒充已验证。

生命周期区分 first_breakout、continuation、failed、monitoring_unknown、watch、none。同一交易日输入变化另标 data_correction，策略参数变化另标 parameter_change。延续期保留第一次突破锚点；使用新复权映射版本在原锚点日期重标定，绝不拿当天滚动高点取代原锚点。旧锚点日期移出缓存或缺因子时监控转未知。当前规则不再命中但旧突破仍有效的证券仍留在全量台账，不当作新的突破触发。

## 使用

现有 iFinD 采集入口保留，新增 AKShare 全市场入口见 [AKSHARE_HISTORY.md](AKSHARE_HISTORY.md)；策略计算层不直接调用供应商接口。历史补齐使用交易日历和验证过的缓存入口：

```bash
python -m china_stock_engine.cli backfill-akshare --sessions 300 --end YYYY-MM-DD
# iFinD 入口仍然可用
python -m china_stock_engine.cli backfill --sessions 300 --end YYYY-MM-DD --with-adjustment-snapshot
python -m china_stock_engine.cli build-report --research-asof 'YYYY-MM-DDT20:15:00+08:00'
python -m china_stock_engine.cli inspect-series --code 600000.SH --sessions 120 --research-asof 'YYYY-MM-DDT20:15:00+08:00'
python -m china_stock_engine.cli record-technical-review --code 600000.SH --date YYYY-MM-DD --decision exclude --reason '核实的风险及证据' --sources 'https://example.com/filing'
```

`inspect-series` 仅读一个证券的 60/120/最多 300 日明细，展示原始 OHLC、统一因子 OHLC、观测时间和缺口，不调用接口。补历史的采集时点仍是真实采集时点：后补历史只能用于后续研究视图，不能声称旧决策时已观测。

研究决策写入内容哈希命名的 `facts/technical_reviews/*.json`，绑定日终扫描哈希、股票/日期、keep/exclude/wait、原因、资料来源和实际复核时间。它保留原始技术候选，不覆盖为“基本面合格股票”；同一候选多次研究也都保留。当前研究视图使用 `record-technical-review --research-view latest`（或显式视图哈希），绑定该视图的扫描与研究截点；不用旧日终记录冒充当时已知。

## Automation 阅读和报告

固定同一 commit，先读 last_run_status、manifest 和 research_context，再读紧凑雷达及必要的 research 分片。按需用 inspect-series 复算，避免向模型输送全市场全年行情。报告交代触发事实、关键价位、可核实催化或“暂未找到”、主要风险/未知项、偏离状态、下一观察条件和原锚点失效条件。

不得强迫价格变化配一个故事，也不因缺财务包取消全市场技术发布。未知风险不是已确认无风险，WATCH 不称“即将启动”，成交额放大不称“主力吸筹”。执行所需的现行交易制度、报价、成本和真实成交条件单独核验。

## 验证边界

全量扫描保存 A0（仅价格突破）、A1（再加成交额）、A2（再加强收盘）的标记与计数，供后续同口径比较。第一版没有收益回测或成交模拟，没有胜率承诺。后续比较 5/10/20 日表现、跌回原锚点、最大不利波动、成本和无法成交比例时，必须使用当时证券范围（包括退市股）、实际资料可得时间与信号后交易窗口；不能假设按信号日收盘买到。

年度缓存只是扫描底座，不能单独证明长期盈利。研究过滤效果须将原始候选与所有保留/排除记录对照，而非只统计通过基本面排雷的股票。


## 本轮验收记录（2026-10-12）

模拟 5,000 只 × 300 日、150 万行数据验证：指标和全市场扫描保留全部 5,000 个命中；该阶段本地耗时约 7.2 秒（不含下载、旧榜单和研究分片构建）。150 个命中进入雷达时展示 100 个，全部命中另行保留。

使用仓库截至 2026-10-09 的真实事实在临时副本构建，5,573 只证券全量扫描与发布验证通过。现有行情只有 44 个实际价格日期，且完整复权与日历认证尚未齐备，全部证券尚不满足主规则的数据门槛。单股明细入口能够返回 44 行并列出未认证日历/缺行情日期；此类日期不能直接等同于停牌或真实开市日。测试与临时构建没有触发真实接口采集，没有改写仓库数据或远端。


2026-10-15 已完成 AKShare 全市场真实历史补齐和正式筛选，最新实际覆盖/候选/缺口见 [AKSHARE_HISTORY.md](AKSHARE_HISTORY.md) 的实际验收；之前 44 日缓存的限制已由本轮补齐解决。

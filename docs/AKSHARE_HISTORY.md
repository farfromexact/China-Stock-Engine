# AKShare 全市场历史与技术筛选

## 使用

安装仓库依赖后，补齐当前全 A 股证券名单下最近 300 个交易日，并发布验证后的技术筛选：

```bash
python -m china_stock_engine.cli backfill-akshare --end 2026-10-12 --sessions 300
```

`--collect-only` 只采集规范化检查点；`--publish-only` 不调用接口，验证已经完成的检查点并构建/发布。`--limit 3 --collect-only` 用于小样本，部分股票池不得发布到 latest。`--workers` 为 1..4，默认 4；全局请求间隔与逐请求超时共同约束网络访问。默认检查点在被 Git 忽略的 `.private-stock-data/akshare-backfill`，可用 `--cache-dir` 指定持久路径。

独立源码启动器为 `scripts/collect_akshare_history.py`。其 `--probe-code 920001.BJ` 可只核验一只股票的配对行情；不会改动批次状态或 latest。

## 数据源与口径

- AKShare `stock_info_a_code_name` 与沪深京交易所股票名单：当前证券代码/名称与上市日期。股票池独立校验三家交易所，不用旧 Top100 替代全市场。
- AKShare `tool_trade_date_hist_sina`：验证返回范围覆盖请求终点，然后保留区间中所有明确开市/休市日期；300 日指交易日，不是 300 个自然日。
- AKShare 腾讯历史行情接口的同一上游、解码器和字段口径：原始和后复权日线分别获取。单个覆盖窗口替代 SDK 中重叠的逐年窗口和额外起始日期查询；全市场读取不保存原始 HTTP 响应。
- 腾讯主板、创业板、北交所成交量按手转换为股；科创板原始量为股，689 CDR 为份，均不再乘 100。成交额从万元转元。通过主板、北交所、科创板的实际样本与量价关系验证，不把 `sz000xxx` 股票误当作指数，并用成交额/成交量对价格范围的宽容差一致性检查拦截单位错误。
- 现有验证过的 iFinD 股本参考可以复用，但保留原参考日期；新股票股本未知不填假数据。不要求全市场财务包。

**这是当前上市股票的回溯行情池，不是历史每一天的全市场 PIT 成员名单。** 它服务当前机会发现；不能直接据此宣称无幸存者偏差的历史全市场回测。退市股票的历史成员池是另外的研究范围。

## 复权 OHLC 与价位映射

腾讯后复权包含现金分红偏移。不能只拿“复权收盘 / 原收盘”乘全部 OHLC。因此同时保留原始和供应商后复权 OHLC，以同一个归一化基准处理整条价格序列，并验证逐日仿射关系：

```text
adjusted_price = raw_price × adj_multiplier + adj_offset
raw_breakout_level = (adjusted_breakout_level − current_adj_offset) / current_adj_multiplier
```

`adj_factor` 保留为有效收盘比例，兼容已有 iFinD 乘法因子；新原生 OHLC 使用 `provider_adj_open/high/low/close`。有直接原生复权价格时优先使用它们，未提供时才按因子乘法处理。原始/复权日期必须完全匹配，价格映射必须在供应商舍入容差内一致，未知/不一致不能补造复权。

每个股票的整条历史使用同一观察版本，保留实际 `known_at`、源价格日期与 `base_date`。先前突破锚点也使用原日期的新版本乘数/偏移重新映射，不重新选择滚动前高。

## 验证与存储

检查点只包含规范化数据，每只股票附内容哈希、实际观测时间与缺失交易日列表。恢复时验证哈希及批次股票池/日历身份；窗口滚动时复用已验证的原始历史、重新获取近期原始行情并刷新整个后复权窗口；失败股票最多重试 3 次，失败原因保留，未完成全市场批次不得提升 latest。

通过检查的历史存放在：

```text
facts/akshare/market_history/vintage=HASH/trade_date=DATE/daily_quotes.parquet
facts/akshare/market_history/vintage=HASH/trade_date=DATE/market_receipt.json
facts/akshare/market_history/vintage=HASH/history_coverage.parquet
facts/akshare/market_history/vintage=HASH/history_manifest.json
facts/adjustment/as_of_date=OBSERVED_DATE/vintage=HASH-PREFIX/adjustment_factors.parquet
facts/akshare/snapshots/vintage=HASH/trade_date=DATE/
facts/akshare/reference/as_of_date=DATE/vintage=HASH/trading_calendar.parquet
```

按日期分行情、按代码前缀分复权，避免单文件过大。历史读取选择研究截点之前真正已观测的最新验证版本；新补采不改写旧 iFinD 价格文件和同日快照，也不冒充过去已掌握的资料。latest manifest 的 source_snapshot_path 明确指向 AKShare 的独立市场快照。

新上市股票自然没有全部 300 日；没有供应商日线的开市日列为未独立核实的停牌/源缺口，不填零或跨缺口凑连续窗口。覆盖报告区分上市前日期、实际观察数、上市后的未核实缺口和请求失败。

最新快照通过原有行情质量门后，在独立目录构建完整的 BREAKOUT/WATCH、研究分片和 manifest，验证通过才提升 latest。后补历史用于现在的研究视图，不能反向写成旧日终决策。状态输出为 `akshare_backfill_status.json`；全量技术台账和紧凑雷达见 [PRICE_VOLUME_RADAR.md](PRICE_VOLUME_RADAR.md)。

## 交易状态与更新

证券名称提供 ST/退市整理的明确分类；有实际成交的完成日线证明当天没有全日停牌。无成交或无行情时仍待核，不把它自动解释成正常可交易。未独立核实的涨跌停制度、手数、下一窗口成交条件保持未知。数学候选可以生成，但入场状态保留待核。

本命令是明确调用的采集入口，不创建新的定时任务。已有 iFinD 日常流程仍保留；新增日期的复权必须由验证过的供应商输入补齐，不能把旧因子擅自延长为已知。再次执行新的 AKShare 300 日窗口会产生新的观察版本；旧版本供审计，长期重复全量归档时需要关注仓库容量。

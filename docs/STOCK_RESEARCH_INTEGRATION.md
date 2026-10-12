# Stock 公司证据研究接口

Stock 发布公司事实、异常、来源与缺口。下游 Radar 负责投资判断、反证、表达比较和稳定 idea_id。候选入选、数据新鲜和研究分片生成成功，均不代表投资优势或当前可执行。

## 两个时间边界

行情的 `trade_date` 与研究允许的信息截点 `research_asof` 分开。原市场 manifest 的采集时间、`configured_decision_cutoff`、`effective_pit_cutoff` 保持原值。`research_context` 给出原市场快照的发布时间（未知为 null）、首次取得时间、当前研究可用性与原决策时点回放可用性；单份财报和公告也附带自己的 `research_availability`。

```powershell
python -m china_stock_engine build-report --research-asof 2026-10-12T09:00:00+08:00 --company-exposures config/company_commodity_exposures.json
python -m china_stock_engine validate
```

时间必须带时区。当前研究只基于最新交易日；截点不能早于该市场快照实际取得时间。后补历史行情需通过原快照 hash 和可得时间校验，晚于截点的财报、公告和可选因子不进入结果。停牌、真实交易日缺口、复权覆盖仍独立限制各窗口，不因放宽研究截点而自动放行。

带 `--research-asof` 的构建在暂存目录完成并通过大小与 hash 检查后只更新 `data/latest/`；不重写 `data/snapshots/`、`data/features/` 或原始 facts。相同数据和相同截点应生成相同字节。每日工作流在补充采集后显式指定研究截点；不开启原本关闭的采集开关。

不带该参数保留旧的历史截止构建行为。旧产物不一定具备严格回放资格：尤其是补抓行情，必须检查 `usable_for_historical_replay`，不能仅依据旧 `effective_pit_cutoff` 或 `data_fresh` 认定当时已经拥有数据。该布尔值仅覆盖标注的事实，不替代整个策略的时点审计。

## 按能力读取

固定同一 Git commit，按顺序读取：

1. `data/last_run_status.json` 和 `data/latest/manifest.json`。
2. `data/latest/data_reference_latest.json` 的 `research_context`、`research_capabilities`。
3. `data/latest/opportunity_radar_latest.json` 的紧凑候选。
4. `data/latest/research_inputs_latest.json`，沿实际 `shards` 字段和候选 `research_context.part` 读取明细，并核对 SHA256。

能力表分别说明单日原始价量、1/3/5/20 日原始和复权收益、行业相对强弱、正式指数宽度、财务现金流、预测修订、公司公告和执行检查。覆盖分母明确；公司宽度不能取自 Top100，市值分组不能当作真实指数成分。

当前尚无完整时点成分覆盖认证，指数宽度不发布 ready。EOD 证券状态即使 clear，也不代表盘中报价、成本和下一交易窗口已经验证。证券状态从同一已校验行情快照和带时点的 provider facts 重建，不沿用手工 debug 标记，不把缺行情解释成停牌。

候选保持原 bucket 轮换、去重和证据族规则，不新增综合投资分数。`research_context` 仅给出分片、证据域、适合继续研究的路线和财务缺口；“经营变化是否尚未定价”由下游研究回答。

## 最小财务集合与真实采集边界

每家公司分片含 `minimum_financial_evidence`：收入、归母利润、扣非利润、经营现金流、资本开支、应收、存货、现金和有息债务的逐项缺口。同一最新报表的收入、利润、OCF 均存在才计入现金流研究覆盖；完整最小集合另计数。净债务仅为有息债务减账面货币资金，未验证受限现金。YTD、单季与 TTM 沿用现有规范化转换，不混算。

金融企业和未知业务类型需专门判断指标适用性，不将工业企业比率作为银行、保险企业质量评分。没有可靠同年度预测版本时，不产生“超预期”结论。

实际检查结果：`ifind_supplemental.py` 目前只采集归母净利润和披露日期；`config/ifind_enrichment.json` 的财报模板只映射归母利润和总资产，行业、证券状态和预测模板仍未配置。重跑不能补齐收入或 OCF。新增供应商字段需要确认官方指标、参数、单位和权限，并通过已有 mapping hash 对应 canary。此次没有猜测收费指标、扩大采集范围或虚构财务值。

深研可显式指定少量公司，继续使用现有缓存、请求预算和 canary 门槛：

```powershell
python -m china_stock_engine.ifind_enrichment --modules financial_history --codes 600000.SH,000001.SZ --max-requests 8
```

上例仅演示命令格式，不是研究推荐。未扩充映射前仍只取得已配置字段。补齐字段也可通过原 `import-research` 接口导入有原始披露依据的规范化报表。

## 公司—商品利润暴露

`config/company_commodity_exposures.json` 当前收录两家深研公司的四条已核对披露暴露，最多 100 条，不按概念板块扩展。每条必须有：

| 字段 | 内容 |
| --- | --- |
| exposure_id / thscode | 唯一暴露标识及证券代码 |
| business_segment / commodity | 具体业务分部与商品或价差 |
| channel | product_price / input_cost / processing_spread / volume |
| direction | positive / negative / conditional / unknown |
| constraints | 非空约束列表，如套保、长协、销量、产能、成本结构 |
| transmission_horizon | 预计体现时间或明确 unknown |
| source_url / evidence_summary | 无认证参数的 HTTPS 披露链接及事实摘要 |
| source_published_at / first_observed_at / known_at | 带时区时间，依次不减 |
| confidence | verified / partially_verified / research_only |

仅 known_at 不晚于研究截点的记录进入对应公司分片，索引记录配置内容 hash。验证器检查结构与时间，不能代替人工核验商业暴露。公司身份和暴露事实在 Stock；具体事件交易假设、方向与生命周期留在现有 Radar。

已下载核对的 PDF 可同时附带 `document_sha256` 和 `source_pdf_pages`（从 1 开始的文件页码，不是印刷页码）。`confidence=verified` 仅表示业务暴露有原始披露依据；本批四条 `direction` 均为 `conditional`，并非确认某个商品变动必然改变集团利润或股价。

## 第二阶段已落地的有限样本

2026-10-12 核对紫金矿业（601899.SH）与宝钢股份（600019.SH）的 2026 半年报，导入 4 条报表观察：两家公司各一条本期 YTD 和一条披露文件内列示的上年同期 YTD。完整最小财务集合覆盖 2 家；不是全市场基本面覆盖，也没有更改候选排序或生成投资质量分数。原文件和 iFinD 原始响应不入库，公开披露的规范化数值及来源凭据入库。

新增 `research_inputs_latest.json.company_evidence_directory`，按证券代码列出具备完整最小财务集合或商品暴露的公司及其分片 hash，最多 100 家，同时报告总数与截断状态。这个入口独立于价量 Top100，覆盖不足不伪装为完整选股结果。新增 `revenue_growth_profit_decline` 等确定性检查路线，只提示继续研究。

字段凭据、口径和复核方法见 [PUBLIC_FILING_EVIDENCE.md](PUBLIC_FILING_EVIDENCE.md)。供应商 HTTP 指标映射未扩充；本次 iFinD 自然语言查询用于小范围交叉核对，不能作为 HTTP 映射或全市场权限已通过 canary 的证明。

## 验收与下一阶段

第一阶段验收：晚抓数据能用于今天但不回写过去；能力分别发布；候选分片可按 hash 定位；超 300 KiB 失败且保留 last valid latest；缺状态不变成可交易。

后续数据工作：扩展已验证财报样本和历史比较期、验证收入/OCF 等供应商映射与权限、接入有效行业与完整时点指数成分。盈利预测和复杂因子后置。20 个交易日影子比较沿用 Radar 归档，记录新公司证据、有效反证、表达变化、执行缺口识别和观点延续，不在 Stock 建立第二套观点台账；20 日结果不证明稳定超额收益。本次未启动影子任务，也未修改现有四个 Automation 的 prompt、启停或调度。

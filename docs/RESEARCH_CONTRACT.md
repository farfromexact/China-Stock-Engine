# Research facts contract v1

目标：向下游提供可追溯事实，而不是选股结论。市场取样是TAPE域；财务和事件发现独立扫描PIT参考股票池，不以进入Top100为前提。

## 已实施与未实施

已实施：规范化批次校验/幂等追加、修订选择、财务YTD/单季/TTM/同比、基础PE/PB、公告状态与价格响应、全股票池分片、manifest绑定、失败关闭。

未实施：iFinD财报/公告自动提取映射、预期修正、行业经营数据、历史估值分位、股息率和同业估值分位。这些字段不会被已有行情替代，也不声称已获取真实财务或公告数据。付费采集必须另行验证指标、口径、权限、增量游标；现有工作流没有为它们增加API请求。

## 规范化输入

`import-research --input <batch.json>` 只接受文档规定字段，多余键（包括raw响应、token）报错。支持 `financials` / `events` 两类。根字段必须完整：

```json
{
  "schema_version": 1,
  "module": "financials",
  "collection_started_at": "2026-09-04T10:00:00+00:00",
  "collection_completed_at": "2026-09-04T10:01:00+00:00",
  "coverage": {
    "thscodes": ["000000.SZ"],
    "complete": true,
    "period_start": "2025-01-01",
    "period_end": "2026-06-30"
  },
  "records": []
}
```

以上代码仅为虚构示例，不是生产数据。`coverage.complete=true` 表示供应商/采集器明确确认查询完整，不可从HTTP成功或空列表推断。financials覆盖区间按报告期；events按公告北京时间发布日期。没有覆盖证据的证券保持unknown。

所有记录必须包含：

| 字段 | 语义 |
| --- | --- |
| thscode | 六位数字加 `.SH/.SZ/.BJ`，必须位于查询覆盖代码中 |
| published_at | 来源实际发布时间，带时区 |
| first_seen_at | 采集系统首次发现时间，不得伪装为历史发布时间 |
| known_at | 可供本系统消费的实际已知时间 |
| revision | 来源修订标识，非空字符串 |
| source_url | 可追溯HTTPS文件/公告链接，不可包含认证信息 |
| document_sha256 | 原文件哈希；无法取得时明确null，不能哈希伪造正文 |

严格要求 `published_at <= first_seen_at <= known_at <= collection_completed_at`。不保存原文件、公告正文或原始iFinD响应。输入中的时间、覆盖与来源须由适配器/操作者提供可靠证据，软件不能自行认证这些声明。

### Financials

在公共字段以外，财务记录必须有：

```json
{
  "report_period": "2026-06-30",
  "period_start": "2026-01-01",
  "period_basis": "YTD",
  "accounting_scope": "consolidated",
  "currency": "CNY",
  "unit": "CNY_1e4",
  "values": {
    "revenue": 100,
    "net_profit_parent": 12,
    "net_profit_ex_nonrecurring": 10,
    "operating_cash_flow": -2,
    "equity_parent": 150,
    "cash": 20,
    "interest_bearing_debt": 30,
    "capex": null
  }
}
```

这是记录专有字段示例，实际记录还须包含全部公共字段。只支持自然年季末、合并口径人民币报表；`CNY/CNY_1e4/CNY_1e8` 分别为元/万元/亿元，统一为元后落盘。金额未知为null，布尔值、非有限数和未知单位拒绝。

营收、利润、OCF、capex为YTD流量；权益、现金、有息债务为期末存量，不能对存量做YTD相减。单季流量=本期YTD−前一季YTD；一季度直接使用YTD。TTM=本期YTD+上年全年−上年同期YTD；年报直接使用全年。缺任何必要报表则结果为空。

同比仅在上年同口径数大于零时计算 `(本期/基期−1)*100`。负/零基数不硬算增长率。正利润但负OCF仅在两值已知时给出布尔事实；扣非占比基于正归母利润。没有“超预期”解释。PE以正归母净利润TTM为分母，PB以正归母权益为分母；这不是公允价值判断。

每个 `thscode+report_period` 选信息截止前最后已知的修订；同known_at存在冲突记录会失败。衍生字段列出参与报表hash；旧版本保留在不可变批次中。

### Events

专有字段为 `event_id, event_type, event_date, status, title`。event_type支持 `filing/earnings_preview/buyback/dividend/shareholder_reduction/share_unlock/control_transfer`；status支持 `announced/approved/in_progress/completed/cancelled/unknown`。event_date可以是未来实施日，不等于已完成；标题最多500字符，不接受正文冒充标题。

事件窗口按最近20个市场会话的发布日期起点至信息截止时间。仅完整覆盖该证券、该区间且确实空结果时，输出 `confirmed_no_events_in_window`；没有完整查询证据为unknown，没有模块为not_ready。

`price_response` 从发布时间之后首个完整开盘日开始，只用已存在的交易日和报价；晚于15:00的公告不归因于当天涨跌。原始收益是供应商日涨跌幅复合，不是复权异常收益、alpha、因果结论或策略PnL；缺任一天行情保持not_ready。

## 时间、分片与发布

- 行情 `effective_pit_cutoff` 不因晚间新增公告而被改写。研究索引另列 `research_effective_pit_cutoff`，取配置截止前可见来源的实际最大完成时间；generated_at同此，不用构建机器当前时间。
- 导入批次以canonical内容SHA命名 `facts/research/<module>/<hash>.json`。批次不可变，重复导入不重写；构建时复验hash、schema和PIT关系。source_batch_hashes绑定明确输入集合。
- `research_inputs_latest.json` 为小索引。代码哈希首字符决定16个固定桶，桶内按代码排序、按240KiB软目标分页，文件名 `research_<hex>_<page>.json`。单个证券记录也不得超过300KiB；超限失败，不能偷偷删事件。未来若事件量需要再细分，应显式升级契约。
- 每页包含source_snapshot_sha256和信息截止时间，索引包含页路径/字节数/SHA256/代码范围。所有入口和页统一进入行情manifest；消费者先读manifest并核对哈希，最好固定Git commit。任何大小/schema验证失败均不得移动latest。
- `feature_input_sha256` 绑定21日窗口及可选特征输入。更新源事实或规则版本可改变结果；相同完整输入和版本重复构建须字节相同。历史Git版本可复现旧取样，不做整段历史重写。

## 后续真实适配器要求

先小范围canary，再按公告/财报更新游标增量采集；必须保留原始公布时间、首次见到时间及修订，不把当前修订值回填过去。本版支持financials的单股指标canary，但不将未经映射/校验的basic-data响应直接转为财务事实。事件HTTP适配器仍需实施。

财务公告正常接入前，研究入口中的空值是诚实的未就绪状态，不是“没有基本面变化”或“没有催化剂”。

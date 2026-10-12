# 公开财报字段凭据与第二阶段样本

本模块将少量已人工核对的公开财报接入现有 `import-research` 接口。它不是自动 PDF 解析器；结构校验不能替代阅读原表。字段转换先与原始 PDF 金额核对，再保存规范化事实。iFinD 小范围查询对主要流量指标提供交叉核对，但格式化的亿元显示值不用于替代精确报表金额。

## 原始披露与范围

| 公司 | 原始披露 | PDF 页码（从 1 开始） |
| --- | --- | --- |
| 紫金矿业 601899.SH | [2026 半年报](https://static.cninfo.com.cn/finalpage/2026-08-22/1225550244.PDF) | 6：主要指标和扣非；34：合并有息债务；42–43：合并资产负债表；45：合并利润表；49：合并现金流量表；8、213：业务和套保约束 |
| 宝钢股份 600019.SH | [2026 半年报](https://static.cninfo.com.cn/finalpage/2026-08-22/1225489250.PDF) | 6：扣非；51：合并有息债务；57–58：合并资产负债表；60：合并利润表；62–63：合并现金流量表；9：售价与原料成本讨论 |

规范化批次：`data/facts/research/financials/0bfa8e0e759a61c7eac5783f1ba57a2551bbb990a0c704cfc5bec42bd029e29e.json`。两个 PDF 的实际 SHA256 存在每条记录及暴露条目中，发布前已逐项核对来源页上的金额。

记录覆盖 2026H1 和当前披露列示的 2025H1 比较值，报表口径均为合并、自然年累计、人民币。资产负债表的上年度末余额不是上年半年度余额，本批不将其塞进 2025H1。由此，收入、利润和现金流同比可计算，应收及存货同比、没有相应完整历史流量的 TTM 和单季变化仍可能为空。

公告只有日期，`published_at` 保守设为披露日 2026-08-22 上海时间日末。`first_seen_at` 为本次实际下载后确认取得的 2026-10-12T01:28:40Z；`known_at` 为本次复核导入时间。上年同期比较值同样只在本次取得后可见，绝不回填为 2025 年已知。实际文件采集 hash 保留，公告日不作为首次取得时间。

## 单位与财务口径

- 原表元金额按原表精度保存。宝钢扣非金额取主要指标表百万元精度，两家公司有息债务取债券章节合并口径、亿元两位小数，即百万元精度。
- 有息债务采用公司明示定义，未把母公司债务替代合并数，也不把若干负债科目机械相加为另一种“有息债务”。跨公司比较仍需检查债务定义差异。
- `capex` 定义为购建固定资产、无形资产和其他长期资产支付的现金，统一为正支出；不包含并购支出，也不取投资现金流净额。紫金表内括号负数需取绝对值。
- `cash` 是报表货币资金，未调整受限现金。最小证据中的净债务按较粗来源精度取整，并发布 `net_debt_precision_cny`；派生比率只是算术结果，不提高原始精度。
- 有完整最小集合不等于审计通过、没有偿债风险、估值便宜或值得买入。例如债务章节还包含到期、逾期等信息，九字段集合不能替代进一步信用核对。

## v2 可选字段凭据契约

原 v1/v2 输入不带新字段时，规范化内容和 batch hash 保持兼容。金融事实的 v2 记录可增加 `field_evidence`，此时必须：

1. `source_url_kind=public_document`、`document_access=public_document_verified`，并填实际下载文件的 `document_sha256`。
2. 每个非空数值都有同名凭据，且不允许多余凭据；每条凭据恰含 `pdf_page`、`label`、`column`、`source_value`、`source_unit`、`precision_cny`、`transformation`。
3. `source_unit` 允许 `CNY`、`CNY_1e6`、`CNY_1e8`；`precision_cny` 是换算为元后的来源分辨率。校验器核对金额转换及精度整除，拒绝单位不符或非有限数。
4. `transformation=identity`，或仅对资本开支允许 `cash_outflow_absolute`。文件页码从 1 开始。标签和列标识必须保留，便于复核合并/母公司、本期/比较期。

导入命令（输入须符合已有 research envelope）：

```powershell
python -m china_stock_engine import-research --input reviewed-financials.json
python -m china_stock_engine build-report --research-asof <带时区且不早于取得时间的截点> --company-exposures config/company_commodity_exposures.json
python -m china_stock_engine validate
```

导入生成不可变内容 hash 批次。重复输入复用旧批次；修改需新批次与真实修订时间。原 PDF、下载临时文件、API 认证参数、原始供应商响应不得提交。今后优先在新财报发布或目标公司进入深研时更新，未加入每日全量抓取、未开启收费指标扫描。

## 商品传导证据

暴露表记录紫金矿产金、矿产铜的产品售价渠道，以及宝钢铁矿石成本、钢材相对原料加工价差渠道。`confidence` 对应披露事实，而 `direction=conditional` 对应研究边界：实现售价、销量、长协、汇率、库存、套保、少数股东权益和成本结构可能改变最终利润。未估计统一利润敏感系数，也未与商品直投作投资优劣比较。后续 Radar 可据此提出或推翻假设，但需补充当时估值、预期和执行条件。

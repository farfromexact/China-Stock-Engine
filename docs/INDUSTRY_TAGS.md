# A 股申万行业标签快照

这份静态 Parquet 为股票打行业标签，按 `thscode` 与本仓股票数据关联。一行一只股票，保留一级、二级和三级行业名称。

- 数据：[a_share_industry.parquet](../data/reference/industry/snapshot_date=2026-10-16/a_share_industry.parquet)
- 来源与校验清单：[manifest.json](../data/reference/industry/snapshot_date=2026-10-16/manifest.json)
- 股票范围：[2026-10-15 股票池](../data/snapshots/2026-10-15/universe.parquet)
- 取得日期：2026-10-16；每条记录的实际取得时间见 `observed_at`。

## 覆盖情况

| 市场 | 股票池数量 | 三级名称均完整 |
| --- | ---: | ---: |
| 上海 | 2,320 | 2,320 |
| 深圳 | 2,906 | 2,906 |
| 北京 | 350 | 350 |
| 合计 | 5,576 | 5,576 |

股票代码无重复，股票池无遗漏。样本内共出现 31 个一级、131 个二级、336 个三级行业名称；后两项是有股票覆盖的名称数量，不代表完整分类字典的类别总数。

例如：贵州茅台为“食品饮料 / 白酒Ⅱ / 白酒Ⅲ”，宁德时代为“电力设备 / 电池 / 锂电池”，贝特瑞为“电力设备 / 电池 / 电池化学品”。标签直接采用本次数据源返回值。

## 来源与口径

来源为 [同花顺 iFinD](https://www.51ifind.com) MCP 股票服务 `search_stocks`，读取“所属一级申万行业”“所属二级申万行业”“所属三级申万行业”。通过已配置的 iFinD 技能调用服务；实际调用入口是本机 `ifind-finance-data/call-node.js`，身份凭证不进入数据或文档。

按股票代码前三位分成 14 组查询，包括 `920` 北交所分组。每组查询形如“股票代码以600开头，所属一级申万行业，所属二级申万行业，所属三级申万行业”。超过 100 行时读取服务提供的 CSV 下载，较小分组解析完整表格；每组均核对导出行数与服务报告的证券数，且不超过 1,000 行导出上限。全市场单次查询会截断，不能据此声明全量覆盖。

供应商分组总共返回 5,581 个代码。与指定股票池按完整代码精确关联后保存 5,576 行，另外 5 个池外代码记录在 manifest 的 `excluded_outside_reference_universe`，不据此推断其上市状态。没有凭简称或代码近似匹配，也没有推测补齐行业。

本次接口没有明确返回分类版本号、行业代码及归属生效日期，因此这些字段为 null。虽然标签采用申万体系，**本文件不宣称已独立核验为“申万2021版官方全量分类表”**。截图中的 `801730` 等行业指数代码不能直接替代未取得的分类代码。[分类代码与指数代码字段说明](https://tushare.pro/document/2?doc_id=181)可供后续完善字典时参考。

## 字段

| 字段 | 含义 |
| --- | --- |
| `snapshot_date` | 标签快照取得日期，Parquet date32 |
| `universe_as_of_date` | 股票池日期，固定为 2026-10-15 |
| `thscode` | 股票代码，含 `.SH`、`.SZ`、`.BJ` 后缀，保留前导零 |
| `security_name`, `exchange`, `board` | 来自指定股票池的名称、交易所、板块 |
| `classification_system` | `SW`，申万行业体系 |
| `classification_version`, `version_status` | 版本未由接口报告：null / `not_reported` |
| `sw1_name`, `sw2_name`, `sw3_name` | 本次观测到的一、二、三级行业名称 |
| `sw1_code`, `sw2_code`, `sw3_code` | 本次未取得，均为 null |
| `effective_from`, `effective_to` | 行业归属有效期未知，均为 null |
| `observed_at` | 实际采集时间，带 UTC 时区的毫秒时间戳 |
| `source`, `source_batch` | 数据源标识、三位代码查询分组 |
| `source_security_name` | 来源当次返回的股票简称，便于与股票池简称核对 |
| `coverage_state` | `observed_names`，只表示行业名称已取得 |

manifest 记录股票池与 Parquet 的 SHA-256、字节数、逐级覆盖、各交易所覆盖、分组查询文本、实际行数、取得时间及来源内容哈希。仅保存规范化字段和来源摘要，不保存原始供应商响应、临时下载地址或凭证。

字符检查发现 `003029.SZ` 吉大正元的原始分组表格中有一条乱码；单股重新查询确认三级行业为“垂直应用软件”，用复查值替换并在 `source_corrections` 中记录查询、时间和来源哈希。最终行业名称不含替换字符。

## 读取和打标签

在仓库根目录运行：

```python
import pandas as pd

tags = pd.read_parquet(
    "data/reference/industry/snapshot_date=2026-10-16/a_share_industry.parquet"
)
stocks = pd.read_parquet("data/latest/universe.parquet")
tagged = stocks.merge(
    tags[["thscode", "sw1_name", "sw2_name", "sw3_name", "observed_at"]],
    on="thscode",
    how="left",
    validate="one_to_one",
)
missing = tagged.loc[tagged["sw1_name"].isna(), ["thscode", "security_name"]]
print(missing)  # 未来股票池新增证券可能缺少这份静态快照的标签
```

一级行业适合汇总和主标签，二三级适合细分筛选。自定义主题、产品和产业链标签应另外保存，避免与供应商行业归属混在同一字段中。

## 时间限制与更新

这是 2026-10-16 取得的当前分类观测，股票池截止 2026-10-15。`observed_at` 不等于分类生效日，也不能据此认定历史交易日已经知道这些行业归属。不得将这份快照回填为历史 PIT 成分、行业超额收益或回测的历史事实。

文件独立保存在 `data/reference/industry/`，没有写入生产的 `facts/classification/industry_membership.parquet`，没有改变日常采集、报表或 Automation。以后更新时新增日期目录、重新核对当时股票池，保留旧快照与 manifest；若接入生产，需要另行验证分类版本、行业代码、有效期与历史可知时间。

本次完成：14 个分组行数核对、股票代码格式和唯一性、一对一全股票池匹配、逐级缺失检查、二三级名称的父级唯一性、Parquet 写入后回读一致性及三只跨市场样本核对。覆盖完整不代表已独立验证供应商每个分类判断。

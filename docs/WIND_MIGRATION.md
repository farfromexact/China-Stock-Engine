# Wind Mac 迁移与验收

## 当前状态

2026-11-11 夜间已在用户确认看板关闭的独占窗口完成真实 Wind 小样本与扩展验证：沪深京、科创板、创业板、CDR 共 6 只、300 个交易日；原始/复权 OHLC 配对、成交量和成交额单位通过。100 代码单字段 × 300 日矩阵已在 Mac 实测。当前全市场发布结果以 `data/wind_publication_status.json` 为准。

实现包括供应商无关接口、单个隔离子进程、串行原生请求、Mac/Windows SDK 路径配置、跨平台文件锁、带 SHA 的断点采集，以及独立 Wind 版本的验证与提升。Windows 原生 SDK 尚未在本仓库实机验收，见 [Windows 接手说明](WINDOWS_WIND_HANDOFF.md)。

本项目不启动或停止另一个仓库的看板。**只要 8765 端口有监听者，Wind 采集就在导入 SDK 前失败。** 无法判断端口状态也失败。`--exclusive-window` 是操作者对完整采集窗口的确认，不绕过端口检查。文件锁不能证明所有其他机器/账号会话都空闲；同账号 Mac/Windows 不要并发取数。

## 环境与无连接检查

官方 Mac SDK 保留在应用的安装位置，不通过 pip 安装同名 WindPy 包，不复制 SDK 或凭据到仓库。本仓库各自建立 `.venv`，只注册官方 SDK 路径：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python scripts/configure_wind.py --check
.venv/bin/python scripts/configure_wind.py
.venv/bin/python -m china_stock_engine.wind_collect doctor
```

配置脚本及 `doctor` 不导入 WindPy，也不登录或取数。登录状态仅在真实独占窗口验收。子进程只允许 WSET/WSS/WSD/TDAYS，只读请求串行，超时不会自动重连；清理只退出自己拥有的子进程，不对共享后端执行 stop/cancel/logout。

## 小样本先行

示例 profile 默认 `units_verified=false`，每台新机器仍必须先实测、审核字段与单位；Mac 已验证的结论不自动变成 Windows 验收。请求保留空值，不向前填充停牌或缺失行情。成交量规范单位为股，成交额为元，股本为股；必须根据官方字段说明和实测标的确认三种倍率，不能靠请求成功就认定单位正确。

在已确认的独占窗口，取沪深京三只标的最近 5 个交易日：

```bash
.venv/bin/python -m china_stock_engine.wind_collect canary \
  --end YYYY-MM-DD --profile config/wind_profile.example.json --exclusive-window
```

默认标的是 `600000.SH,000001.SZ,920001.BJ`，可通过 `--codes` 换成当前 Wind 股票池确认的代码。随后扩展验证科创板、创业板、新股、停牌、分红送转标的。需要核对历史股票池、上市日期、日历、错误/权限码、字段单位和缺失语义。SSE 日历为首个适配口径，沪深京日历一致性仍需实测，不把已验证的期货日历当作股票证据。

原始与原生后复权 OHLC 分别获取，要求键集合一致且整组 OHLC 满足同一日乘法映射；不满足时拒绝生成复权因子，不采用只比较收盘价的捷径。因子 known_at 为实际观察时间，不回写成过去已知。当前适配器对非乘法复权映射拒绝通过。

## 独立 300 日候选

先把示例复制为 `.private-stock-data/wind/profile.json`，根据真实 canary 与官方字段定义确认单位、字段和复权参数后，才把本机 profile 的 `units_verified` 改为 true，并用该 profile 再跑 canary。未审核单位时全市场请求会在连接 SDK 前被拒绝；不要为了绕过检查直接改布尔值。

```bash
.venv/bin/python -m china_stock_engine.wind_collect collect \
  --end YYYY-MM-DD --sessions 300 \
  --profile .private-stock-data/wind/profile.json --exclusive-window
```

候选与检查点只保存在 `.private-stock-data/wind/`，不提交原始 SDK 响应；规范化 Parquet 检查点逐只落盘并保存哈希。重跑相同 profile/日期窗口复用通过哈希校验的结果。供应商对相同窗口修订行情时用 `--refresh` 放弃旧检查点。改变窗口或单位配置会重新验证，不拼接旧供应商复权因子。

全市场模式要求至少 5000 只且涵盖沪深京；小样本永远标记 `canary_only=true`，不能冒充全市场。观察到的 K 线单独保存，上市前不要求补线，上市后缺失日期记录为未确认，不猜测为停牌、不造 K 线。候选通过现有市场质量门、报告/筛选生成及最终 hash 校验；后补历史只用于明确研究截点，不冒充历史日终信息。每次构建有独立目录，失败不会更新生产 latest。

已实测 Mac 的多代码、单字段矩阵，最多每批 100 代码，严格校验代码/时间/字段轴；原生请求仍串行。`--batch-size 1` 可回到逐代码请求。实际额度和 Windows 批量能力仍需各机验收，不能套用看板期权批量。新窗口当前完整重取，未声称已实现每日增量优化。

## 自动化和切换条件

新增 `Wind Mac Candidate Validation` 工作流，仅手动 dispatch，且默认因 `WIND_MAC_CANDIDATE_ENABLED` 未设置而跳过；每次还必须显式确认 `exclusive_window`，默认 false。需要专用标签 `wind-stock` 的 Mac ARM64 self-hosted runner，使用自己的 checkout/.venv；当前未安装或启动 runner、launchd 任务，也未更改 ChatGPT 调度桥。runner 检查点放在用户目录的 `Library/Application Support/ChinaStockEngine/wind`，避免 checkout 清理掉断点；审核后的本机 profile 也放在那里。候选工作流无仓库写权限，不发布、不回退调用 iFinD。

## 经过验证再提升生产

```bash
.venv/bin/python -m china_stock_engine.wind_publish \
  --candidate .private-stock-data/wind/candidates/CANDIDATE_ID --data-dir data
.venv/bin/python -m china_stock_engine.cli validate --data-dir data
```

发布器拒绝样本、未审核单位、不完整 300 日历史、质量或哈希失败的候选。在独立暂存目录复制现有数据并重建研究报告，通过后才提升；失败保留旧 latest。新行情、参考与复权保存为 `facts/wind/vintage=...`，最新 manifest 的 `source_snapshot_path` 明确指向对应 Wind 快照。旧 iFinD/AKShare 事实、快照和技术台账逐文件校验未变。

当前 Wind 研究与序列检查只选择 Wind 的复权批次，不隐式回退其他供应商因子。`known_at` 为实际观察时间，历史研究仍受截点限制。重复提升同一候选幂等。全量版本仍需容量监测，未来增量和 Wind 版本留存策略应在正式无人值守部署时验收，不能关闭容量门后无限累积。

## 后续自动采集切源

本轮可手动生成并提升完整 Wind 数据，**不等于已部署无人值守 Wind 自动采集**。Mac/Windows self-hosted workflow 都默认关闭、手动 dispatch、候选只读，未安装 runner/launchd/Windows service。两个 workflow 共用原生会话 concurrency group，防止 GitHub 同仓重复任务并发；本机与跨机器账号占用仍需独占窗口协调。

旧 iFinD 日常与补充任务暂时保留，避免 runner 和登录尚未就绪时停更。正式停用需先验收本机 SDK 登录、独占窗口、重启恢复、增量追赶、版本留存/容量以及 commit/push 发布，再切换 ChatGPT 调度桥。财务/事件等尚未映射的可选模块明确保持原来源或未知，不冒充 Wind 数据。

## 2026-11-11 全市场实测结果

- 交易日范围：2025-08-14 至 2026-11-11，共 300 个交易日。
- 当前全 A 股票池 5580 只，与原 iFinD 当日池代码集合一致。
- 日线 1,643,988 条；5393 只具备完整 300 日，187 只上市较晚；按上市日核对，未确认的上市后缺失日期为 0。
- 最新 5574 只正常成交，6 只为供应商返回的无成交价格观察（成交量/额为 0），保留 `quote_without_turnover`，不伪装成成交。
- 与原 iFinD 当日可比的 5574 只收盘价全部一致，成交量差异仅浮点精度级别。
- 全市场扫描 5580 行：25 个 BREAKOUT、11 个 WATCH。它们是待核验研究候选，不代表执行许可。
- 发布后 1634 个既有历史事实、快照和特征文件 SHA 未变，活动数据约 529.5 MB，低于 900 MB 容量门槛。
- 完成市场质量、派生产物大小/hash、完整 300 日历史读取与生产 CLI validate。详情见 `data/wind_publication_status.json` 与 `data/storage_status.json`。
- 本轮本机测试 173 项通过；新增 GitHub Windows/Ubuntu 离线 CI 用于验证代码与 Windows PowerShell 语法，仍不等于 Windows 原生 Wind SDK 实测。

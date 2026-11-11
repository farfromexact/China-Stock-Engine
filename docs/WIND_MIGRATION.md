# Wind Mac 迁移与验收

## 当前状态

2026-11-11 用户确认：看板会持续运行，本轮仅保留离线改造，不执行真实 Wind 连接，不启用 runner 或生产切换。

已实现供应商无关的行情接口、WindPy 适配器、独立子进程与串行请求、Mac SDK 路径配置、带 SHA 校验的断点采集和独立候选数据构建。现有 iFinD 日常工作流和生产 `data/` 未切换；不把离线测试当作真实 Wind 权限或全市场验收。

当前看板仍使用自己的 Wind 会话。本项目没有通用会话代理可复用，也没有修改另一个仓库或看板。**只要 8765 端口有监听者，Wind 采集就会在导入 SDK 之前失败。** 无法判断端口状态也会失败。`--exclusive-window` 是操作者对独占时间窗口的确认，不会绕过端口检查。

此检查及 stock collector 文件锁并不能证明机器上所有其他 Wind 客户端都已退出。真实采集必须安排在看板和其他客户端不使用 Wind 的独占窗口；不要仅因页面显示“收盘”就运行。看板运行期间不要执行连接探测脚本、另开 `w.start()` 或调用 `w.stop()`。

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

示例 profile 的字段及单位仍是**待实测配置**，`units_verified=false`。请求保留空值，不向前填充停牌或缺失行情。成交量规范单位为股，成交额为元，股本为股；必须根据官方字段说明和实测标的确认三种倍率，不能靠请求成功就认定单位正确。

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

单代码 WSD 请求是初始保守实现，不声称已验证全市场耗时、频率和账户额度。下一轮按真实 canary 结果再优化批量矩阵；不要套用期权批量大小。新窗口当前会完整重取，而非已优化的每日增量。

## 自动化和切换条件

新增 `Wind Mac Candidate Validation` 工作流，仅手动 dispatch，且默认因 `WIND_MAC_CANDIDATE_ENABLED` 未设置而跳过；每次还必须显式确认 `exclusive_window`，默认 false。需要专用标签 `wind-stock` 的 Mac ARM64 self-hosted runner，使用自己的 checkout/.venv；当前未安装或启动 runner、launchd 任务，也未更改 ChatGPT 调度桥。runner 检查点放在用户目录的 `Library/Application Support/ChinaStockEngine/wind`，避免 checkout 清理掉断点；审核后的本机 profile 也放在那里。候选工作流无仓库写权限，不发布、不回退调用 iFinD。

**尚未启用生产 Wind 发布。** 切换前必须完成：

1. 独占窗口真实连接及字段/单位验收；完成沪深京、缺失/分红边界验证。
2. 全市场 300 日候选完成，并与原数据在重叠区间做差异分析。
3. 建立生产 Wind 数据版本的归档/提升事务，保留原 iFinD/AKShare 历史文件与旧因子来源；不得直接覆盖已有历史日期分区。
4. 实测增量追赶、重复运行、复权更新、失败保留旧 latest、容量检查及远端发布。
5. 切换 ChatGPT 调度桥与数据源选择，同时停用 iFinD 的行情和补充模块调用。映射未完成的财务/事件字段保持未知，不冒充 Wind 数据。

目前这些是后续切换准入条件，不能因为代码能离线运行就把现有 iFinD 定时采集关闭，否则会制造新的停更。

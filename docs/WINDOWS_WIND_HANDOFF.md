# 给明天 Windows Codex 的 Wind 接手说明

请先读根目录 AGENTS.md，再核对本说明与 `data/wind_publication_status.json` 的实际状态。不要仅凭“Wind 已连接”判断提数完整。

**最新接手任务：** 本地每日/开机补采与 Dot 接口已写好，详见 [本地 Wind 调度](WIND_LOCAL_DAILY.md)。明天先完成本机 canary，再配置私有 local_daily.json 并实测 tick；可选安装交互式用户计划任务。GitHub 的自动取数与 bridge 继续关闭，不能拿恢复远端触发代替本地配置。不要影响 trading desk；看板运行就等待。

## 配置依据与证据边界

依据用户已有 `trading-desk-wind-API` 项目的 `scripts/configure_wind.py`、`docs/CROSS_PLATFORM.md`、Windows 安装脚本及对应 chat 的交接。该项目旧 Windows 版本有真实运行记录；本仓库新增股票批量适配及 Windows 文件锁分支仍需要 Windows 实机复验。

使用 Python **3.12 x64**，官方 Wind 客户端、官方 Python 插件和具备 API 权限的登录会话。SDK 不是 pip 包；不要安装网上同名 WindPy。默认搜索 `%USERPROFILE%\Wind\Wind.NET.Client\WindNET\x64`，但最终以本机含 `WindPy.py` 的真实目录为准。

不要复用 trading desk 的 `.venv`，也不要从 Mac 拷贝环境、登录状态、SDK 或私有检查点。SDK 原生调用只在本仓库的单个隔离子进程执行。Mac 和 Windows 使用同一数据规范、策略、复权验证与发布逻辑。

仓库 `.gitattributes` 对 `data/**` 禁用文本换行转换，保证 JSON/Parquet 的字节 SHA 不因 Windows checkout 改变。若旧工作树曾把数据 JSON 转为 CRLF，先检查本地修改并确认没有待保留数据，再恢复对应已提交文件的原始字节；不要通过修改 manifest 哈希“修复”校验。

## 安装与无连接检查（PowerShell）

先检查本地修改，再拉取 main。不要用 reset --hard 清理用户内容。若其他看板正在使用 Wind，只能做安装和 `doctor`，不要执行 canary。

```powershell
git status
git pull --ff-only
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-wind-windows.ps1
# 非默认 SDK 位置：
# .\scripts\setup-wind-windows.ps1 -WindPythonDir 'D:\Wind\Wind.NET.Client\WindNET\x64'
.\.venv\Scripts\python.exe -m china_stock_engine.wind_collect doctor
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

安装脚本只建立本机环境、注册 `.pth`、检查路径，不导入 SDK、不自动登录、不停止看板。官方客户端/API 保持用户登录；首次真实调用若出现独立授权窗口，由用户完成。当前证据来自桌面用户会话，不保证注销后或 Windows service 会话能正常登录。

## 先做本机小样本

确认 Mac 上的提数已结束，同账号不要跨机器同时采集。确认本机看板与其他 Wind 程序不占用 SDK；`--exclusive-window` 不能绕过端口保护。

```powershell
.\.venv\Scripts\python.exe -m china_stock_engine.wind_collect canary `
  --end YYYY-MM-DD --batch-size 3 `
  --profile config/wind_profile.example.json --exclusive-window
```

`YYYY-MM-DD` 选择已经收盘的实际交易日。默认取沪深京三只、最近 5 个交易日；再覆盖科创板、创业板、CDR、新股、停牌/缺失和分红边界。Windows 首轮使用小批量；确认矩阵轴和实际耗时后再提高，最多 100，不套用看板期权批量。

把示例 profile 复制到 `.private-stock-data/wind/profile.json`，验证成交量为股、成交额为元、股本为股，核对原生复权 OHLC 乘法映射后，才能把本机 `units_verified` 标记为 true。不要为了跳过门槛直接改布尔值。Mac 的样本验证和这台机器的登录检查都不能代替 Windows 单位验证。

## 增量样本与日常入口

每日 tick 已默认增量。完成本机单位 canary 后，再按 [Wind 增量更新](WIND_INCREMENTAL.md) 的 PowerShell 命令，用 incremental --codes 验证当前基线复用、两个单字段收盘探针和配对下载；样本不能发布。保留 300 日工作集，完整 K 线按新增/缺口下载，修订按股票重取；非收盘深度修订通过 20 组轮换核查，不能承诺当日全发现。首次新交易日的实际耗时与 Windows 原生返回还需实测。日常使用 wind_daily tick，不要把下面的全量基线命令注册成每日任务。

## 全市场基线重建与生产发布

先读取远端 `data/wind_publication_status.json` 的截止日期。若明早尚无新的已收盘交易日，直接复用已合并的 Wind 300 日数据；配置 SDK 只需要小样本，不要为安装验证重复下载全市场历史。日常增量入口可直接从远端带来的已验证 Wind 版本复用，无须复制 Mac 私有缓存。

```powershell
.\.venv\Scripts\python.exe -m china_stock_engine.wind_collect collect `
  --end YYYY-MM-DD --sessions 300 --batch-size 100 `
  --profile .private-stock-data/wind/profile.json --exclusive-window
```

候选与规范化断点在 `.private-stock-data/wind/`，不包含原始 SDK 响应。相同窗口通过 SHA 的断点可复用；供应商修订相同日期时使用 `--refresh`。此 collect 命令仍完整获取，限首次建库、经审核的 profile 更换或显式基线重建；日常不要使用它。增量吞吐仍需实测，不承诺固定完成时间。

确认候选覆盖完整股票池与 300 个交易日、质量/筛选通过后，才用 `wind_publish` 提升；它拒绝样本、未核对单位或不完整历史。

```powershell
.\.venv\Scripts\python.exe -m china_stock_engine.wind_publish `
  --candidate '.private-stock-data\wind\candidates\候选ID' --data-dir data
.\.venv\Scripts\python.exe -m china_stock_engine.cli validate --data-dir data
git status
```

手工基线发布仍按用户要求 commit/push。日常 wind_daily tick 已默认开启 auto_git_push：验证后自动提交生成数据并推送 origin/main；需先配置本机 Git 身份/推送权限并确保 main 和工作区状态正确。上传失败保留 journal/commit，下一次先重试 Git，不重复取数。不要提交 `.venv`、SDK、登录凭据、临时日志和私有缓存。发布失败保留旧 latest，不修改旧供应商事实；不允许失败后暗中改用 iFinD。

2026-11-12 Mac 已实测单日期多代码 WSD 返回 Data=1×代码数，多日期为代码数×时间数。适配器现同时严格校验这两种形状，并用 3 股票单日原始/复权矩阵与逐股 OHLC 完成真实对照。Windows 仍需本机验收；可在已授权窗口使用 `scripts/probe_wind_coexistence.py --date YYYY-MM-DD --login-confirmed --matrix --profile .private-stock-data/wind/profile.json` 做不可发布小样本，不把 Mac 结果当作 Windows 证明。

## 自动化尚需单独部署

本机无人值守需要已登录 Wind、开机在线、不休眠且存在独占窗口。当前方案用本地 tick 与可选交互式计划任务，不需要自托管 runner；仍不能假定 Windows service 可复用桌面登录。不要擅自安装系统服务。

Mac 和 Windows 的 GitHub workflow 只是默认关闭的手动候选验证，不负责本地每日任务。用户已明确要求远端 iFinD 设为 idle，日常/补充/研究增强/Scheduler Bridge 均已禁用，仓库变量 `IFIND_COLLECTION_STATE=idle`，继续保持。后续使用本地 tick、Dot 的短时就绪确认及可选计划任务；先实测本机 SDK、独占窗口、任务触发、睡眠/重启恢复和发布，不要把代码合并当成已完成无人值守部署。相关实际状态以本地 status、README 和 workflow 配置为准。

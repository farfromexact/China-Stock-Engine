# 本地 Wind 每日采集与开机补采

> 2026-11-12 更新：新增用户授权的显式看板共存模式，见 [共存采集](WIND_DASHBOARD_COEXISTENCE.md)。下文端口阻挡/独占条件适用于默认旧模式。

GitHub iFinD 日常、补充、研究增强及 Scheduler Bridge 继续 disabled，`IFIND_COLLECTION_STATE=idle`。本功能不 dispatch Actions、不安装 self-hosted runner。按用户 2026-11-12 授权，本地采集成功并通过验证后默认自动 commit/push 数据；使用本机已有 Git 身份与凭据，不自动登录 GitHub。Mac/Windows 共用 `china_stock_engine.wind_daily`。

## 运行规则

- `tick` 在开机登录后和定时调用时执行同一套判断。上海时间 **18:00** 前只处理此前已完成交易日，18:00 后才考虑当日。工作日只是上界，真正日期和节假日取 Wind `tdays`；缓存不能把未知工作日推断成已开市。
- 检查当前已发布 Wind 版本最近 300 个交易日的逐日行情和状态文件 SHA。最新日期相同但历史分区缺失/损坏，也要补采；不拿旧 iFinD/AKShare 行情掩盖 Wind 缺口。已经齐备则不连接 SDK、不重复下载。
- 每日默认增量：复用已发布 Wind 历史，仅取新增/缺失日期；同时查询整窗原始/复权收盘探针，并每次轮换一组（20 组中的一组）股票的完整字段来发现深度修订。受影响股票单独修复，最终在本地拼成完整 300 日候选。复权、核查频率和人工全量核查例外见 [Wind 增量更新](WIND_INCREMENTAL.md)。上市前不造线，缺失/停牌语义延续原采集器。
- 每台机器必须先完成官方 SDK、单位和复权小样本验证。`units_verified=false` 在原生连接前阻止全市场采集。Windows 的离线 CI 不能代替真实 canary。
- Trading Desk 的 **8765 端口有监听时直接等待**，不调用 `w.start/stop/logout/cancel`，不停止、重启或修改看板。SDK 子进程进入及每次请求仍检查端口；同时要求操作者/Dot 事先保留完整独占窗口。端口检查无法协调同账号的另一台机器；Mac 与 Windows 只指定一台采集，不要同时授权。
- 同一机器的定时、开机和 Dot 触发使用同一个 OS 文件锁，崩溃自动释放。原生调用另有全局 Wind 会话锁。失败后默认等待 15 分钟再试，不快速重连。
- 完整候选落盘后记录 pending；发布/容量失败或进程中断后，下一次先校验并恢复该候选发布，不重复登录。中途下载失败复用原采集器 SHA 检查点。质量/容量失败不移动 latest；无 iFinD 回退。

## 配置

先按 `WINDOWS_WIND_HANDOFF.md` 或 `WIND_MIGRATION.md` 完成本机 SDK canary。复制 `config/wind_local_daily.example.json` 到 `.private-stock-data/wind/local_daily.json`，核对 profile 的本机路径后，把本地配置 `enabled` 改为 true。所有相对路径均相对仓库根目录，不依赖调用者当前目录；不要把此私有配置提交到 Git。

Mac 的 `PYTHON` 是 `.venv/bin/python`，Windows 为 `.\.venv\Scripts\python.exe`。以下以 Mac 为例：

```bash
# 不连接 SDK；显示 up_to_date / needs_calendar / catchup_required / idle。
.venv/bin/python -m china_stock_engine.wind_daily check
# 一次调度尝试；没有有效的本机就绪确认时只等待。
.venv/bin/python -m china_stock_engine.wind_daily tick
```

`check` 不改生产和状态 JSON，也不调用 Git 或联网上传。`tick` 将结果写入 `.private-stock-data/wind/local_daily_status.json`，常用状态为 `idle`、`up_to_date`、`waiting_dashboard`、`waiting_login_and_exclusive_window`、`waiting_exclusive_window`、`retry_wait`、`collecting`、`publishing`、`published`、`failed`、`git_push_pending`。`missing_dates` 是本次检测/补采的日期，不是补采后仍缺失的日期。`failed` 的 `error` 和 `next_retry_at` 供人工排查；API 错误不写账户响应。

## 验证后自动提交、推送

`auto_git_push` 默认 true，一键启动器调用 tick 同样生效。必须在本仓库 main、对应的 origin、已配置 Git 提交身份和推送权限下运行；不切换分支，不把用户的代码或已暂存改动一起提交。仅允许当前已验证 Wind 版本、latest、相应技术研究视图、发布状态及明确退役的每日版本路径。推送到 `origin/main`，不自动 force-push、rebase 或覆盖远端并发更新。

成功结果的 `git.state=pushed`、`git.commit` 是本次上传的提交；`no_changes` 表示无新本地数据提交，不制造空提交。若 `auto_git_push=false`，published 仍只代表本地发布；自定义数据目录/仅本地实验应显式关闭此项。单独运行 `wind_collect` 或 `wind_publish` 不会自动 Git 推送。

推送失败返回 `git_push_pending`（非零退出码），本地数据、已生成 commit 和 `.private-stock-data/wind/git_pending.json` 均保留。下一次 tick 在进入 Wind SDK 前先恢复上传，不重复下载；没有 journal 但上次验证后的生成数据尚未提交时也会先尝试发布。存在用户改动、分支错误、远端推进或数据变化时会停下供人工同步，不擅自处理冲突。不要通过删除 journal、reset --hard 或强推来“清除”此状态。代码改动先完成提交并同步主线，再启动每日采集。

## Dot 应怎样接手

Dot 负责从本机官方客户端的真实状态确认 **Wind 已登录且 API 可用**，并协调这台机器和其他机器/程序的独占时间。仅见到应用进程、端口或桌面登录，不能认定 API 已登录。本仓库没有替 Dot 实现 GUI 登录监视器，也不会为了“检查登录”不断导入 SDK 或弹出登录框。

确认登录并保留独占窗口、Trading Desk 未运行之后，再授权一个短窗口并立即尝试：

```bash
.venv/bin/python -m china_stock_engine.wind_daily arm \
  --login-confirmed --exclusive-window --minutes 60
.venv/bin/python -m china_stock_engine.wind_daily tick
```

`arm` 不连接 SDK；写入本机 hostname 绑定、最长 60 分钟的私有就绪文件。过期、跨机器、任一确认缺失或运行中撤销，都会阻止下一个原生请求。Dot 只能在持续确认条件满足时续期；不能每轮无条件 arm。观察到退出登录、看板启动或另一台机器取数，应撤销：

```bash
.venv/bin/python -m china_stock_engine.wind_daily disarm
```

建议 Dot 每 5 分钟调用一次 `tick`；18:00 前可以补此前漏日，18:00 后采当日。发布成功、采集失败或 Git 上传待恢复时才提醒；是否上传检查嵌套 git 状态。默认旧模式在看板持续占用时等待；用户授权的共存模式见专门说明。超过 300 个交易日的长期离线历史不属于此自动窗口，应人工安排专项回补。

## Windows 计划任务（明天实机配置）

只使用当前已登录的交互式 Windows 用户，不使用 SYSTEM、管理员权限或“无论用户是否登录都运行”的服务账户，不存密码。请在 SDK canary、配置和本地 tick 实测后再安装：

```powershell
# 默认只是预览，未安装或执行采集。
.\scripts\install-wind-local-task.ps1
# 用户已授权的本地计划任务：登录时 + 每 5 分钟，睡眠后补错过的触发。
.\scripts\install-wind-local-task.ps1 -Install
Get-ScheduledTask -TaskName ChinaStockEngine-Wind-Local
# 暂停/撤销：
.\.venv\Scripts\python.exe -m china_stock_engine.wind_daily disarm
.\scripts\install-wind-local-task.ps1 -Remove
```

安装脚本不覆盖已有同名任务，不导入 SDK、不自动 arm。即使已注册任务，没有 Dot/人工的短时就绪确认也不会启动原生连接。不唤醒休眠电脑；注销、重启后登录恢复、API 授权窗口、实际批量耗时和任务退出码需在 Windows 实测。Mac 本轮未安装 launchd/后台进程；可由 Dot 调用同一入口，不必再装一套 GitHub runner。

触发器参数及无 Duration 的重复规则依据 Microsoft 的 [New-ScheduledTaskTrigger](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger) 和 [Task Scheduler repetition Duration](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-duration-repetitiontype-element) 文档；这不代替目标 Windows 的实际注册验收。

## 容量与留存

原来的全量发布追加每个 300 日 vintage，会迅速重复占用空间。本地每日入口调用发布器的 `rolling=True`：**仅轮换标记为 `local_daily_rolling` 的后续每日 Wind 版本，保留最新一份完整 300 日行情、复权、日历及最新扫描**。迁移时原始 Wind 版本与既有 iFinD/AKShare/快照/历史特征均受保护；手工发布默认仍为 immutable，不能由此入口扫除。当前单个基线加一份每日版本有明确上限，900 MB 活动数据门继续生效，异常增长会失败而非静默删旧供应商数据。

轮换在私有暂存副本完成，经筛选、质量、SHA 和容量门全部通过才切换生产；失败时现有版本不变。生产 receipt 记录 `retired_local_daily_vintages`。这是一份滚动工作集，历史版本需要通过正常数据 commit 保存到 Git 历史，不能假定未提交的本地每日版本永久保留。私有目录只轮换本入口登记的候选，保留当前一份，手工 canary 不清扫；下载检查点继续复用。

## 验收边界

已加入离线测试：收盘时间/时区、节假日、增量缺口、复权/历史修订、重复触发、看板阻挡、登录/独占确认失效、任务互斥、失败退避、pending 发布恢复、300 日候选和轮换发布事务。真实 Wind 300 日基线沿用 2026-11-11 的已发布证据；本轮不为测试调度或增量算法抢占 SDK。Windows 原生增量、计划任务注册、真正开机恢复与下一交易日自动更新仍待当地实机验收。


## 2026-11-12 晚最新默认（用户授权，优先于旧独占说明）

日常入口默认 allow_dashboard=true、allow_parallel_collectors=true、request_interval_seconds=0。股票/商品可同时取数，单仓库内部仍串行；本仓库 daily/session 防重复锁、profile 审核、日期/字段校验、独立发布及推送保护全部保留。不开启远端任务、不控制 Trading Desk，不调用stop/cancel/logout。运行间隔独立于已审核profile，不改变profile哈希或字段单位。低层诊断工具仍可显式采用互斥模式。并行配置生效不等于所有交易时段与Windows已验收。

# 本地 Wind 每日采集与开机补采

GitHub iFinD 日常、补充、研究增强及 Scheduler Bridge 继续 disabled，`IFIND_COLLECTION_STATE=idle`。本功能不 dispatch Actions、不登录 GitHub、不自动 commit/push，也不安装 self-hosted runner。Mac/Windows 共用 `china_stock_engine.wind_daily`。

## 运行规则

- `tick` 在开机登录后和定时调用时执行同一套判断。上海时间 **18:00** 前只处理此前已完成交易日，18:00 后才考虑当日。工作日只是上界，真正日期和节假日取 Wind `tdays`；缓存不能把未知工作日推断成已开市。
- 检查当前已发布 Wind 版本最近 300 个交易日的逐日行情和状态文件 SHA。最新日期相同但历史分区缺失/损坏，也要补采；不拿旧 iFinD/AKShare 行情掩盖 Wind 缺口。已经齐备则不连接 SDK、不重复下载。
- 漏几天都生成一个截止最新已收盘交易日的 300 日完整候选，一次补齐窗口内的中间日期，不逐日重复下载整窗。当前仍是完整原始/复权 OHLC 重取，**尚未实现每日增量请求**。上市前不造线，缺失/停牌语义延续原采集器。
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

`check` 不改生产和状态 JSON。`tick` 将结果写入 `.private-stock-data/wind/local_daily_status.json`，常用状态为 `idle`、`up_to_date`、`waiting_dashboard`、`waiting_login_and_exclusive_window`、`waiting_exclusive_window`、`retry_wait`、`collecting`、`publishing`、`published`、`failed`。`missing_dates` 是本次检测/补采的日期，不是补采后仍缺失的日期。`failed` 的 `error` 和 `next_retry_at` 供人工排查；API 错误不写账户响应。

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

建议 Dot 每 5 分钟调用一次 `tick`；18:00 前可以补此前漏日，18:00 后采当日。`published` 或 `failed` 才提醒，`up_to_date` 和等待状态不刷屏。`published` 后核对生产日期和 `cli validate`，再按既有仓库流程提交数据；本入口不自动修改 Git 分支或推送。远端关闭且本机睡眠/离线/未登录/看板持续占用时不会更新，恢复就绪后补窗口内的漏日。超过 300 个交易日的长期离线历史不属于此自动窗口，应人工安排专项回补。

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

已加入离线测试：收盘时间/时区、节假日、漏日/损坏修复、重复触发、看板阻挡、登录/独占确认失效、任务互斥、失败退避、pending 发布恢复、300 日候选和轮换发布事务。真实 Wind 300 日采集沿用 2026-11-11 的已发布证据；本轮不为测试定时器再抢占 SDK。Windows 计划任务仅做语法和离线检查，真正开机恢复与下一交易日自动更新仍待当地实机验收。

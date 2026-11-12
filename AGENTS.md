# China Stock Engine 接手规则

- 同一个 Windows/macOS 仓库与共同 main；不复制策略、字段映射或发布逻辑为两套平台代码。
- 开始先读 README、`docs/WIND_MIGRATION.md`；Windows 接手还要读 `docs/WINDOWS_WIND_HANDOFF.md`。检查本地修改与远端状态，避免覆盖用户未提交内容。
- Wind SDK 来自官方客户端，不从 PyPI 安装同名包，不把 SDK、账号、密码、refresh token 或许可证写进 Git。每台机器创建自己的 `.venv`，不要复制 Mac 虚拟环境到 Windows。
- SDK 路径用 `scripts/configure_wind.py` 注册；Windows 默认仅是 `%USERPROFILE%\Wind\Wind.NET.Client\WindNET\x64`，必须以本机 `WindPy.py` 实际位置为准。
- 未经用户授权，不停止或重启其他看板/服务。真实 Wind 请求走本仓库隔离进程与串行会话，先检查 8765 端口；无法确认空闲也不能绕过检查。
- Mac 的 100 代码单字段矩阵及 300 日采集证据不能代替 Windows 实机验收。Windows 先 small canary，确认字段、单位、日历和复权再全市场；小样本不能提升到生产。
- 不覆盖已有 iFinD/AKShare 日期事实、快照或旧技术台账。Wind 发布使用独立 `facts/wind/vintage=...`，验证后才更新 latest；后补数据 known_at 是真实观察时间。
- 本地每日入口见 `docs/WIND_LOCAL_DAILY.md`：只轮换明确标记 local_daily_rolling 的新 Wind 版本，迁移基线和旧供应商历史不删。Dot 的登录确认必须同时保留完整独占窗口，不能只因进程存在就自动 arm；远端触发继续 idle。
- 每日默认增量，详见 `docs/WIND_INCREMENTAL.md`；复用仅来自当前已验证 Wind vintage，新增/缺口成对下载原始/复权 OHLC，整窗收盘探针 + 20 组完整字段核查。不要把两个单字段探针描述成零历史请求，也不要承诺非收盘深度修订当日全发现；全量修订核查需显式 --audit-all。
- 修改后运行 `python -m unittest discover -s tests` 以及变更涉及的真实验收；区分模拟测试与实机结果。实际数据发布前跑 CLI validate、检查容量及差异。
- GitHub 托管 Ubuntu 不视为已支持本机 Wind 登录。Mac/Windows self-hosted workflow 默认关闭；不得擅自安装系统服务或启动后台 runner。用户已明确将远端 iFinD 工作流及 Scheduler Bridge 设为 idle，不得擅自恢复；Wind 手动功能保持可用。

## 2026-11-12 用户授权更新（优先于上文旧端口限制）

用户明确要求允许 Trading Desk 运行时低速取数。新增显式 `allow_dashboard=true` 模式，
详见 `docs/WIND_DASHBOARD_COEXISTENCE.md`；默认旧模式保持兼容。
股票/商品日常原生 worker 已使用同一跨仓库本机用户锁；不得声称覆盖所有旧 backfill 或跨机器。
不得为了共存关闭/重启/注销看板或 Wind；小样本验收后才把共存模式接入一键启动器。

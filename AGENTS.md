# China Stock Engine 接手规则

- 同一个 Windows/macOS 仓库与共同 main；不复制策略、字段映射或发布逻辑为两套平台代码。
- 开始先读 README、`docs/WIND_MIGRATION.md`；Windows 接手还要读 `docs/WINDOWS_WIND_HANDOFF.md`。检查本地修改与远端状态，避免覆盖用户未提交内容。
- Wind SDK 来自官方客户端，不从 PyPI 安装同名包，不把 SDK、账号、密码、refresh token 或许可证写进 Git。每台机器创建自己的 `.venv`，不要复制 Mac 虚拟环境到 Windows。
- SDK 路径用 `scripts/configure_wind.py` 注册；Windows 默认仅是 `%USERPROFILE%\Wind\Wind.NET.Client\WindNET\x64`，必须以本机 `WindPy.py` 实际位置为准。
- 未经用户授权，不停止或重启其他看板/服务。真实 Wind 请求走本仓库隔离进程与串行会话，先检查 8765 端口；无法确认空闲也不能绕过检查。
- Mac 的 100 代码单字段矩阵及 300 日采集证据不能代替 Windows 实机验收。Windows 先 small canary，确认字段、单位、日历和复权再全市场；小样本不能提升到生产。
- 不覆盖已有 iFinD/AKShare 日期事实、快照或旧技术台账。Wind 发布使用独立 `facts/wind/vintage=...`，验证后才更新 latest；后补数据 known_at 是真实观察时间。
- 修改后运行 `python -m unittest discover -s tests` 以及变更涉及的真实验收；区分模拟测试与实机结果。实际数据发布前跑 CLI validate、检查容量及差异。
- GitHub 托管 Ubuntu 不视为已支持本机 Wind 登录。Mac/Windows self-hosted workflow 默认关闭；不得擅自安装系统服务、启动后台 runner 或关闭仍承担提数职责的旧任务。

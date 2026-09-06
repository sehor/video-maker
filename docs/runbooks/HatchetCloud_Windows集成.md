# Hatchet Cloud Windows 集成

适用：WINDEV-04；API 和 Worker 运行于 Windows，PostgreSQL 使用既有原生实例。
不安装或启动 Docker、WSL、Hatchet Server，不切换真实 RunPod 路由。
当前远程测试尚未获得真实 Cloud 通过结果，见
[WINDEV-04 验证记录](../reports/WINDEV-04_HatchetCloud集成验证.md)。

## 1. 凭据创建与保存

使用独立开发租户，由有权限的管理员在租户 API Token 管理界面创建开发用 Token。
管理员权限与 Token 数据访问范围见 [Hatchet 用户角色说明](https://docs.hatchet.run/v1/user-roles)。
Token 可能读取工作流 payload，因此不要复用生产租户或提交客户素材进行本次验收。

将 Token 写入忽略提交的根 `.env` 的 `HATCHET_CLIENT_TOKEN`，或写入仓库外的
私有文本文件，再设置 `HATCHET_CLIENT_TOKEN_FILE`。文件仅包含 Token，可有末尾换行；
不要在命令参数、聊天、Issue、日志或截图中展示它。文件方式优先，文件缺失或为空时
直接失败，不退回环境变量中的另一个 Token。自建凭据文件不一定被 Git 忽略，优先放仓库外。

```dotenv
WORKFLOW_BACKEND=hatchet
HATCHET_CLIENT_TOKEN_FILE=C:/Users/your-name/private/hatchet-dev.token
HATCHET_CLIENT_TLS_STRATEGY=tls
HATCHET_CLIENT_NAMESPACE=video_maker_dev
```

地址默认由 Token 提供：[官方说明](https://docs.hatchet.run/v1/from-temporal-to-hatchet)。
只有租户明确提供替代入口时才覆盖：`HATCHET_CLIENT_HOST_PORT=grpc-host:443`，
`HATCHET_SERVER_URL=https://console-host`。前者不能含协议前缀，后者不能含 API 路径、
查询参数或凭据。`HATCHET_SERVER_URL` 是本项目配置名。

可选 `HATCHET_CLIENT_TLS_ROOT_CA_FILE` 配置私有 CA，
`HATCHET_CLIENT_TLS_SERVER_NAME` 配置证书服务器名；通常均应省略，使用系统默认信任链。
原生命令将 Token/CA 相对路径统一解析到仓库根。TLS 默认开启，未知策略直接失败。
`none` 仅允许非生产环境的显式 localhost/127.0.0.1/::1/Compose `hatchet` 主机，
不能用于远程 Cloud；生产环境禁止 `none`。没有“忽略证书验证”选项。

## 2. 启动和验收

先按根 README 配好相同的 API/Auth 数据库 URL，运行 `./scripts/dev.ps1 check`。
API 与 Worker 必须使用同一数据库、存储路径、Claim Secret 和命名空间。
三个终端分别执行 `./scripts/dev.ps1 worker`、`./scripts/dev.ps1 api`、
`./scripts/dev.ps1 web`。Local 开发无需这些 Cloud 配置和独立 Worker。

自动验收只需 `./scripts/dev.ps1 test-hatchet`，要求已有、独立、以 `_test` 结尾的
`TEST_DATABASE_URL`。每次测试独立 schema，不重建既有业务表或清理开发库。
普通 `test-api` 不触发 Cloud。显式请求 Cloud 但无 Token 时返回非零退出，不计为验收通过。
Token 文件指定但不可读、TLS 不合法、认证失败或运行超时均应视为失败。

远程测试带 `live` 和 `database` marker，显式 pytest 开关为 `--run-live`。
建议只用原生命令开启，以保留数据库 URL 和隔离检查。测试在 Windows 进程中运行 FastAPI
TestClient，另起 Windows Worker，使用唯一 `windev04_*` 命名空间和 Mock Provider；
经过 API → Outbox → Cloud durable workflow → provider child task → PostgreSQL，
检查 Job 成功、一个可读 MP4、一次结算和同一幂等键复用 Cloud run ID。
这不是 Web/Auth 或真实 GPU 验收。

Worker 就绪等待上限 90 秒，Job 完成等待 120 秒，结果/重复提交各 30 秒。
Worker 日志位于忽略提交的 `.test-runs/live-*/pytest/` 内；排错时先检查并脱敏，
不公开原始 SDK 日志。测试结束只清理自己创建的 Worker 进程树，不停止其他开发进程。
Cloud 的工作流定义和运行历史不会自动删除；在开发租户按该次 `windev04_*` 命名空间
核对和清理，避免误删日常任务。测试本身可能产生 Cloud 用量。

## 3. 故障排查与撤销

依次检查 Backend、Token 文件可读性、URL 格式、Token 地址与手工覆盖是否一致、TLS
主机名/CA 和网络连通性；不要将所有连接错误直接判为凭据错误，也不要通过关闭 TLS 排错。
确认这些条件后，再检查租户 Token 的有效性、撤销状态和权限。

轮换时先停止使用旧凭据的 API/Worker，在开发租户生成新 Token，更新私有文件或 `.env`，
用新进程执行远程验收，随后在租户 Token 管理界面撤销旧 Token。
若已泄露，应立即撤销旧 Token，再生成替代 Token；删除本地文件本身不等于云端撤销。
最后删除不再使用的本地副本，确认仓库中没有 Token。精确按钮名称以当前 Cloud 控制台为准。
彻底停止 Cloud 开发时撤销对应 Token，移除本地配置并恢复 `WORKFLOW_BACKEND=local`。

## 4. Windows SDK 边界

本项目锁定 `hatchet-sdk==1.38.1`。其 Worker 使用 Windows 缺失的 `SIGQUIT`，
其 listener 使用 Proactor 不支持的 `loop.add_signal_handler`。
`app/windows_hatchet_worker.py` 仅在 Windows Backend Worker 路径加载：父 Worker 使用
INT/TERM/BREAK；spawn listener 内安装兼容事件循环，退出仍由 SDK 的进程事件和队列控制。
不修改 SDK 安装目录，不改变 API 事件循环策略，也不改变 Linux Worker 工厂。

适配依赖锁定 SDK 的内部启动接口，仅支持提供 `slot_config` 的现代引擎；旧引擎明确失败。
升级 SDK 时必须重跑 Windows 信号、启动失败和真实 Cloud 测试，不得只据离线通过就解除该限制。
离线测试覆盖 TLS 通道选择、真实 SDK listener 构造、信号回调和启动异常退出，
不能证明真实证书握手、远程注册、心跳或 durable 执行已通过。

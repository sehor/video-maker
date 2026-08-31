# AI 视频镜头工厂

## Windows 原生开发

日常开发不需要 WSL、Docker、Hatchet Token 或独立 Worker。默认使用随 API
运行的 Local Runner，仅适用于开发；生产仍使用 Hatchet。原生入口不会安装、
启动或重启 PostgreSQL，也不会安装浏览器。

前置条件：PowerShell 7、Python 3.12+、uv、Node 22+、pnpm 9.12.3、已运行的
Windows PostgreSQL，以及 PATH 中的 FFmpeg/ffprobe。前端只使用根目录的
`pnpm-lock.yaml`。首次准备依赖：

```powershell
uv sync --project apps/api --extra dev
pnpm install --frozen-lockfile
# E2E 一次性前置安装，普通 e2e 命令不会下载浏览器
pnpm --filter @video-factory/web exec playwright install chromium
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑根目录 `.env`，保留你本机实际账号和密码。API URL 必须以
`postgresql+psycopg://` 开头；Better Auth 使用 `postgresql://`。二者的用户名、
密码、主机、端口和数据库须一致，并与 `POSTGRES_*` 一致。空密码写作
`postgres:@localhost`；用户名和密码的特殊字符需 URL 编码，例如 `@` 写成 `%40`。
`POSTGRES_*` 不会自动补进 URL。密钥和本机 `.env` 不提交。

```powershell
./scripts/dev.ps1 check
# 首次 check 报告缺少迁移属于预期；确认显示的数据库目标后运行
./scripts/dev.ps1 migrate
./scripts/dev.ps1 check
```

`check` 只检查依赖、URL、两个数据库客户端的连接、Alembic head 和 Auth 表，
不修改数据库。`migrate` 先做相同连接检查，再执行 Alembic 和 Better Auth
迁移；不会清库。包含未知表的无版本库、会删除旧表的阶段一升级会被拒绝，
需要先人工检查和备份。两个迁移工具各自提交事务，不提供跨工具的原子迁移；
修复失败原因后可重跑。

在两个终端分别启动，使用 Ctrl+C 结束各自进程：

```powershell
# 终端一
./scripts/dev.ps1 api
# 终端二
./scripts/dev.ps1 web
```

访问 <http://localhost:3000>；API 健康检查为 <http://localhost:8000/healthz>，
就绪检查为 <http://localhost:8000/readyz>。`AUTH_JWKS_URL` 指向
`http://localhost:3000/api/auth/jwks`。Web 需启动，才能完成注册、登录和 JWT 认证。

所有原生命令统一从根 `.env` 读取配置，显式进程环境变量优先。环境文件按
dotenv 解析，不执行 PowerShell，不展开 `${...}`；密码中的 `$` 保持原样。
`STORAGE_ROOT=./data/storage` 始终相对仓库根解析，与调用目录无关。
可用 `-EnvFile <路径>` 指定另一份本机配置；相对配置路径也以仓库根为基准。

## 测试与检查

先用已有 PostgreSQL 管理工具建立独立测试库，例如 `video-maker_test`，然后
在 `.env` 填写完整的 `TEST_DATABASE_URL`。脚本不自动创建数据库；测试库名
必须以 `_test` 结尾，且不能等于开发库名。**API 测试会重建测试库业务表**，
不要与另一个测试进程共享该库。迁移测试内部的 SQLite 库位于临时目录。
每次原生 API 测试的缓存与临时文件位于忽略提交的
`apps/api/.test-tmp-native/run-*`，不会复用系统中旧的 pytest 临时目录。

```powershell
./scripts/dev.ps1 test         # PostgreSQL API 回归 + Web 单元测试
./scripts/dev.ps1 test-api     # 只跑 API；后面可追加 pytest 文件或 -k 参数
./scripts/dev.ps1 test-web
./scripts/dev.ps1 lint
./scripts/dev.ps1 typecheck
./scripts/dev.ps1 build
./scripts/dev.ps1 generate-client
./scripts/dev.ps1 e2e          # 先启动 api/web；使用已安装的 Chromium
```

E2E 默认访问 `http://localhost:3000`，会在当前开发库创建测试账号、项目与镜头，
不会清理已有用户数据。没有浏览器时按上文安装一次；普通 E2E 不启动 Compose。
Local 模式不需要运行 `worker`。可选的 Hatchet Cloud 模式见下节。

Makefile 的日常目标是 PowerShell 入口的别名，默认目标只显示帮助。
容器只用于显式集成验证，先按维护文档预检并复用已有资源，再将
`.env.compose.example` 复制为忽略提交的 `.env.compose`。不要将其中的 `postgres`、
`web` 主机名或 `/data/storage` 路径复制到原生 `.env`。

```powershell
docker compose --env-file .env.compose --profile integration config --quiet
# 首次使用或源码/依赖变化后显式构建；恢复已有容器使用 start
docker compose --env-file .env.compose --profile integration build
docker compose --env-file .env.compose --profile integration up -d --wait
docker compose --env-file .env.compose --profile integration stop
```

Makefile 提供 `compose-config`、`compose-build`、`compose-up`、`compose-start`、
`compose-stop` 目标，例如 `make compose-up`；原 `compose-dev` 是 `compose-up` 的兼容别名。
无 profile 的 Compose 不启动任何服务；显式点名服务仍是集成操作。
容器不挂载源码、不提供热更新，Web 运行构建后的 Nitro 服务；本地开发继续使用上面的
PowerShell 命令。未删除已有数据卷，旧 `web-node-modules` 卷不再挂载，也不会自动清理。

CI 分为 `native-checks` 和 `integration`：前者直接运行 uv/pnpm、SQLite、FFmpeg、
OpenAPI 和 Web 门禁；后者在独立临时 Compose 项目里验证 PostgreSQL 空库迁移、
三轮并发账本、Hatchet 幂等与构建产物 E2E。原 `test` 状态汇总两组结果，任何一组失败
都不能通过。CI 清理只针对本次运行创建的卷，不提供本机清库目标。
实施结果和未执行的远程门禁见
[WINDEV-05 验证记录](docs/reports/WINDEV-05_Compose隔离与CI验证.md)。

## 可选 Hatchet Cloud 集成

在忽略提交的根 `.env` 配置开发租户的 `HATCHET_CLIENT_TOKEN` 或
`HATCHET_CLIENT_TOKEN_FILE`，将 `WORKFLOW_BACKEND` 改为 `hatchet`，并设置
API/Worker 共用的 `HATCHET_CLIENT_NAMESPACE`。默认 TLS 为 `tls`；Host 与
Server URL 默认沿用 Token 内地址，不要填写 localhost 覆盖 Cloud 地址。
Token 文件优先于直接配置的 Token；相对路径以仓库根为准。

```powershell
# 在三个终端分别启动，API 与 Worker 使用同一数据库、存储和命名空间
./scripts/dev.ps1 worker
./scripts/dev.ps1 api
./scripts/dev.ps1 web
# 独立验收：使用 TEST_DATABASE_URL，自动启动测试 Worker，无需上述进程
./scripts/dev.ps1 test-hatchet
```

`test-hatchet` 即使日常 Backend 为 `local` 也会单独使用 Cloud，按次生成独立
命名空间，检查真实耐久工作流、子任务、输出和重复提交复用。它会重建指定测试库
业务表，并在 Cloud 留下测试工作流和运行记录；不应使用生产租户或共享测试库。
无凭据时明确输出 `SKIP`，不连接数据库或 Cloud；普通测试默认跳过远程用例。
已配置但无效的凭据应报错，不会自动退回 Local。

当前已加入锁定 SDK 的 Windows 信号适配；真实 Cloud 验收仍待配置 Token 后执行。
凭据创建、撤销、TLS 故障排查和兼容边界见
[Hatchet Cloud Windows 集成手册](docs/runbooks/HatchetCloud_Windows集成.md)。

## SIM-05 模拟验收状态

SIM-05 已完成并标记为 `SIMULATION_ACCEPTED`：Submit、Poll、Webhook、Retry、Cancel、
Finalize、Ledger、Project、Storage、Dead Letter 和 Readiness 的故障矩阵已使用 Fake Clock、
模拟 Provider／Storage、Fault Injector 与媒体 fixture 验收。PostgreSQL 空库迁移、并发账本
回归连续 3 轮和 Playwright 完整业务闭环均已实际通过。

该状态不代表真实 RunPod、GPU、网络、对象存储、视频质量、性能或成本已经验证。
真实 RunPod／worker-comfyui 路线仍为 `CONDITIONAL`／默认禁用。

- [SIM-05 模拟故障注入验收报告](docs/reports/SIM-05_模拟故障注入验收报告.md)
- [模拟控制面故障恢复 Runbook](docs/runbooks/模拟控制面故障恢复.md)

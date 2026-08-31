# WINDEV-03｜Windows 原生命令验证

日期：2026-08-31。分支：`codex/windows-native-development`。起始提交：`244cb23`。

状态：WINDEV-03 已完成。代码、隔离库验证及用户授权后的开发库迁移/启动验证通过。
原生命令实现提交：`9bf6085`。

## 修改范围

- 新增 `scripts/dev.ps1`，由 `scripts/dev.py` 统一加载根 dotenv 和调度原生命令。
  覆盖 check、migrate、api、web、worker、test/test-api/test-web、e2e、lint、
  typecheck、build、generate-client；不安装或启动数据库、Docker、WSL 或浏览器。
- `.env.example` 使用 localhost 和仓库内相对存储路径；容器配置移至独立
  `.env.compose.example`。Makefile 的日常目标调用 PowerShell，容器入口显式命名。
- 只读预检先校验 API/Better Auth URL、明确的空密码、编码、连接目标及
  `POSTGRES_*` 一致性，再分别用 psycopg 3 和 node-postgres 连接。
  不打印完整连接串，不把网络或数据库不存在问题一概归因为凭据错误。
- 拒绝能覆盖目标的 URL 查询参数，仅支持可选 sslmode。环境文件不作为
  PowerShell 执行，不做变量插值；进程环境优先，存储路径相对仓库根解析。
- 测试必须显式配置独立 `_test` 数据库，且名称不能等于开发库。每次运行
  使用独立 `.test-tmp-native/run-*`，避免旧 Windows 临时目录权限残留。
- 迁移只执行 upgrade，不清库；拒绝有未知表的无版本数据库，以及会删除旧表的
  `0001_stage_one` 升级。API 和 Auth 迁移分别提交，不承诺跨工具的原子性。
- 修正 Alembic ConfigParser 对 `%` 的插值，保留 URL 编码后的字符；迁移测试
  使用含 `%` 的 SQLite 路径回归验证。没有修改业务状态机、账本或认证逻辑。

## 实际环境与验证

Windows 原生 Python 3.13.7、Node 22.16.0、pnpm 9.12.3、PostgreSQL 16.10、
FFmpeg/ffprobe 8.0.1。未调用 WSL 或 Docker，也未安装依赖或浏览器。

| 检查 | 结果 |
|---|---|
| 安全检查与迁移定向测试 | 24 项通过，27.52 秒 |
| 新 `test-api` 入口：安全检查、迁移、Local Runner 实际闭环 | 27 项通过，41.92 秒 |
| PowerShell 转发 pytest `-k empty` | 3 项通过、19 项未选中 |
| 新 `test-web` 入口 | 1 项通过 |
| 新 `lint` / `typecheck` / `build` 入口 | 全部通过 |
| 新 `generate-client` 入口 | 成功，OpenAPI 与 Client 无差异 |
| 独立 PostgreSQL 空库迁移 | Alembic 到 `0011_control_plane_recovery`，Auth 五张表创建成功 |
| 隔离库重复迁移 | Alembic 无新版本、Better Auth 报告 No migrations needed，预检再次通过 |
| 从 `apps/api` 子目录调用 `check` | 正确加载根配置，两个客户端连接及迁移检查通过 |
| 原生 API/Web 启动 | 隔离库下 `/healthz`、`/readyz` 均为 200，四项就绪检查全为真 |
| 新 `e2e` 入口 | 现有 Playwright 业务闭环 1 项通过，19.7 秒 |
| 停止本次 API/Web 进程 | 8000、3000 端口均不再监听 |

数据库隔离如下：

- `video-maker_windev03_test`：空库迁移与 API 定向测试，业务表允许测试重建。
- `video-maker_windev03_e2e_test`：独立迁移及 API/Web/E2E，存储目录名含空格。
  浏览器生成的账号、项目、镜头和 Mock 产物仅保留在此隔离环境。
- `video-maker`：首轮只读连接验证；用户授权继续后已完成迁移及原生启动验证，
  未在开发库运行会重建业务表的 API 测试。

本机忽略提交的 `.env` 只修正了剩余容器默认值（JWKS、存储路径），并配置
独立测试库 URL。原有开发数据库用户名、密码及两个已连通 URL 均保持不变。
临时环境文件、日志、媒体和缓存不纳入提交。

## 开发库补充验收

首轮开发库迁移曾被自动审批拦截。用户在明确迁移对象与“不清库”的边界后，
要求“继续余下的”，本次据此完成剩余操作，没有绕过迁移安全检查。

2026-08-31，直接使用根 `.env`，未覆盖开发数据库凭据：

| 检查 | 实际结果 |
|---|---|
| 迁移前只读预检 | 目标 `localhost:5432/video-maker`，PostgreSQL 16.10，public 无表、无 Alembic 版本，安全检查通过 |
| `scripts/dev.ps1 migrate` | 成功；Alembic 到 `0011_control_plane_recovery`，Better Auth 五张表创建成功 |
| `scripts/dev.ps1 check` | 成功；psycopg 3 与 node-postgres 均连接开发库，迁移和 Auth 表检查通过 |
| `scripts/dev.ps1 api` / `web` | 使用根 `.env` 的原生 API 与 Nuxt/Better Auth 启动成功 |
| API `/healthz`、`/readyz` | HTTP 200；database、migrations、storage、workflow 四项均为 true |
| Web `/login`、`/api/auth/get-session` | HTTP 200；匿名会话返回 null |
| 停止验证进程后 | 8000、3000 端口均不再监听 |

开发库没有已有业务表需要保留或清理；本次未创建测试账号、项目或生成任务，
没有运行测试表重建、清库或数据删除操作。此前隔离库的 E2E 结果仍保持其
原有范围，不表述为本次在开发库重新运行了 E2E。

## 剩余事项与限制

本次没有重跑完整 API 全量测试，使用与改动相关的定向回归；WINDEV-02 的
218 项通过记录不冒充本次结果。E2E 沿用现有的 SUCCEEDED、视频元素可见等
断言，不等同于新增连续播放/解码验证。Hatchet Cloud、Compose/CI 改造及
WSL 全部关闭前后状态的总验收仍属于 WINDEV-04～06。

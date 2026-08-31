# Windows 原生开发环境改造计划

> 状态：`ACCEPTED`，Windows 原生必选门禁通过；按需云集成明确跳过。
> 日期：2026-08-31（计划始于 2026-08-30）
> 背景：SIM-05 已完成；本计划只改造开发环境边界，不改变已经验收的业务语义。

2026-08-31：WINDEV-01 已实现 Backend 配置、工厂、Hatchet 延迟注册和生产保护；
WINDEV-02 已接入正式 Local Runner、FastAPI lifespan、就绪检查和 Reconciler。
本地生成沿用同一 `GenerationExecutionService`，不需要 Hatchet Token 或独立
Worker。WINDEV-03 已实现 Windows 命令入口和环境分离，并在隔离 PostgreSQL
库验证迁移、API/Web 与 E2E；用户授权后，本机 `video-maker` 开发库的迁移、
API/Web 启动与就绪验证也已完成。WINDEV-04 已实现 Cloud TLS 配置、Windows Worker
信号适配与显式远程测试入口；真实 Cloud 验收待开发租户 Token。WINDEV-05 已实现
Compose 显式隔离、构建产物启动和 CI 门禁分组，并通过本机相关门禁。
Docker 构建、Compose 运行和远程容器 CI 未执行，作为独立集成验证记录，
不列为 Windows 原生改造的剩余必做项。WINDEV-06 总验收与文档切换已完成：
API 266 项通过、3 项按环境或显式启用条件跳过；三轮 PostgreSQL 并发检查、
Web 门禁与原生 E2E 通过，WSL 在验收前后均为 `Stopped`。
Hatchet Cloud 按需验证，无凭据时按矩阵明确跳过，不将跳过记为云验收通过。
具体结果见 [WINDEV-01 验收记录](reports/WINDEV-01_Backend配置与数据库URL检查.md)
和 [WINDEV-02 验收记录](reports/WINDEV-02_LocalRunner验收.md)，以及
[WINDEV-03 验证记录](reports/WINDEV-03_Windows原生命令验证.md)和
[WINDEV-04 验证记录](reports/WINDEV-04_HatchetCloud集成验证.md)，以及
[WINDEV-05 验证记录](reports/WINDEV-05_Compose隔离与CI验证.md)。最终结果见
[WINDEV-06 原生总验收](reports/WINDEV-06_Windows原生总验收.md)。

## 1. 目标

日常开发在 WSL 和 Docker 全部停止时，仍能在 Windows 10 上完成：

1. 启动 FastAPI、Nuxt 和 Better Auth；
2. 连接现有 Windows PostgreSQL，并执行 Alembic 与 Better Auth 迁移；
3. 使用本地存储、FFmpeg 和 Mock／模拟 Provider 跑通生成闭环；
4. 执行 API、Web、类型检查、静态检查、OpenAPI Client 和 Playwright 测试；
5. 按需连接 Hatchet Cloud 验证耐久工作流，但不启动本地 Hatchet Server；
6. 将 Docker 限定在 CI、集成验证、发布镜像和 RunPod／ComfyUI Worker 镜像。

### 可量化完成条件

- 日常开发命令不调用 `wsl.exe`、`docker` 或 `docker compose`。
- WSL 所有发行版保持 `Stopped` 时，Windows 原生验收矩阵全部通过。
- 默认开发模式不要求 `HATCHET_CLIENT_TOKEN`，也不启动独立 `hatchet-worker`。
- Hatchet 集成模式下，API 和 `hatchet-worker` 都作为 Windows 进程运行，仅 Hatchet Server 使用云服务。
- `ENVIRONMENT=production` 时配置 `WORKFLOW_BACKEND=local` 必须启动失败。

### 验收边界（2026-08-31 澄清）

- 本次交付是 Windows 原生开发环境；验收不安装、启动或依赖 WSL/Docker。
- WINDEV-05 负责解除默认开发入口与 Compose 的耦合，并保留显式集成配置；
  容器构建、Compose 联调和远程 CI 的实际运行属于独立集成／发布验证，不阻塞本次原生验收。
- 未执行的容器或云测试须如实记录，不能将本机通过推断为这些环境也通过。

## 2. 默认假设与决策

以下内容是本计划的实施基线；若需改变，应在开始对应 Issue 前确认：

- Windows 10 是主要开发环境，PowerShell 7 是开发命令入口。
- 复用已经安装的 Windows PostgreSQL；项目不负责安装、启动或删除该实例。
- PostgreSQL 继续是 Job、Attempt、Outbox、Provider Event 和账本的唯一业务真相。
- 默认开发使用 `WORKFLOW_BACKEND=local`；它只承诺开发闭环，不冒充 Hatchet 的跨进程耐久保证。
- Hatchet Embedded 当前不作为 Windows 方案：项目锁定的 SDK 不支持 `win32/AMD64` sidecar。
- 需要真实耐久工作流时使用 `WORKFLOW_BACKEND=hatchet` 连接 Hatchet Cloud。
- CI 可以在第一轮继续使用 Linux/Docker；本计划首先消除本机开发依赖，再隔离 CI Compose。
- RunPod／ComfyUI Worker 仍按发布镜像运行，不迁移为 Windows 本地服务。

## 3. 当前问题

业务代码的大部分组件已经能在 Windows 运行，WSL 依赖主要来自开发入口和配置耦合：

| 位置 | 当前问题 | 改造方向 |
|---|---|---|
| `Makefile` | 开发、迁移、测试、检查、构建全部进入 Compose | 改为 Windows 原生命令；容器命令移到显式集成目标 |
| `.env.example` | 数据库使用 `postgres`，JWKS 使用 `web`，存储使用 Linux 路径 | 默认样例改为 `localhost` 和 Windows 可用相对路径 |
| `compose.yaml` | 同时塞入 PostgreSQL、Hatchet、API、Worker 和 Web，并挂载源码 | 从默认开发入口移除，改为集成／发布用途 |
| `public_api.py` | 固定创建 `HatchetWorkflowStarter` | 根据配置创建本地或 Hatchet Backend |
| `hatchet_workflows.py` | 导入时即创建 Hatchet Client，且 TLS 固定为 `none` | 延迟创建；区分本地 Hatchet 与 Hatchet Cloud TLS |
| `worker.py` | 只能作为 Hatchet Worker 启动，但常被误认为必须用容器 | 保留为 Windows 可直接启动的 Hatchet 集成进程 |
| Playwright | CI 安装与执行路径依赖容器栈已启动 | 浏览器在 Windows 一次安装，测试直接启动 Windows Web/API |

## 4. 目标开发架构

```text
Windows PostgreSQL  <---- FastAPI (Windows / uv)
       ^                         |
       |                         +---- LocalObjectStorage (Windows 目录)
Nuxt + Better Auth               +---- FFmpeg / ffprobe (Windows 可执行文件)
(Windows / pnpm)                 |
                                 +---- WORKFLOW_BACKEND=local（默认开发）
                                 |
                                 +---- WORKFLOW_BACKEND=hatchet（按需）
                                            |
                                   Hatchet Cloud
                                            |
                                   hatchet-worker (Windows / uv)
```

### 4.1 三种运行模式

| 模式 | PostgreSQL | API/Web | 工作流 | Docker/WSL |
|---|---|---|---|---|
| 日常开发 | Windows 本机 | Windows 本机 | `local`，随 API 生命周期运行 | 不需要 |
| Hatchet 集成 | Windows 本机 | Windows 本机 | Hatchet Cloud + Windows Worker | 不需要 |
| CI／发布 | CI 服务或外部数据库 | 构建产物／容器 | Hatchet 耐久路径 | 允许显式使用 |

## 5. 核心改造设计

### 5.1 Workflow Backend 边界

新增配置：

```dotenv
WORKFLOW_BACKEND=local
```

允许值仅为：

- `local`：默认开发模式；进程内调度已有 `GenerationExecutionService`。
- `hatchet`：Hatchet Cloud、CI 集成或生产模式；继续使用现有稳定幂等键启动耐久工作流。

实现要求：

1. 增加统一的 `create_workflow_starter(settings)`，`public_api.py` 不再固定实例化 Hatchet。
2. 把测试目录中的 `InlineWorkflowStarter` 提升为正式但仅限开发的 `LocalWorkflowStarter`，测试和开发复用同一实现。
3. `LocalWorkflowStarter.start()` 必须快速返回稳定的本地 workflow ID，后台循环调用既有 `GenerationExecutionService`，不得复制结算、账本或状态机逻辑。
4. 本地任务由 FastAPI lifespan 管理，关闭时可取消；进程崩溃后的恢复继续依赖 PostgreSQL 和现有 Reconciler。
5. 相同 `idempotency_key` 不得创建多个本地执行任务。
6. `/readyz` 按当前 Backend 检查：`local` 检查本地 Runner 已启动，`hatchet` 检查 Client 配置和启动能力。
7. `production + local`、Hatchet 配置缺失、Cloud TLS 配置错误都必须明确失败，不能静默降级。

### 5.2 Hatchet 的开发定位

- 不在 Windows 本机自托管 Hatchet Server。
- 不在日常开发启动 `hatchet-worker`。
- 需要验证 durable task、child task、重放和远程调度时，连接 Hatchet Cloud。
- Hatchet 集成模式使用 Windows 命令启动 Worker：

```powershell
./scripts/dev.ps1 worker
# 独立测试库上的远程验收（无凭据时明确 SKIP）
./scripts/dev.ps1 test-hatchet
```

- Hatchet Client 改为延迟初始化，并支持 Cloud 所需的 TLS；Token 只从本机未跟踪的 `.env` 或安全变量读取。
- CI 至少保留一组带 `integration` marker 的 Hatchet 契约测试，防止本地 Backend 与生产路径漂移。

### 5.3 Windows PostgreSQL

开发环境默认连接：

```dotenv
DATABASE_URL=postgresql+psycopg://postgres:@localhost:5432/video-maker
BETTER_AUTH_DATABASE_URL=postgresql://postgres:@localhost:5432/video-maker
```

这里按用户更新后的 `.env.example` 使用用户名 `postgres` 和数据库 `video-maker`，
不推断用户名是否拼写错误。两个 URL 必须指向同一开发库，但 API 的 SQLAlchemy
需要显式选择 `psycopg` 驱动，Better Auth 的 node-postgres URL 不带 `+psycopg`。
用户名和密码中的特殊字符必须 URL 编码。`POSTGRES_*` 不会被 API 自动补入 URL。
公开样例不保存实际口令：`POSTGRES_PASSWORD=`，URL 中 `postgres:@` 表示空值。
本机 `.env` 的两个 URL 必须与当前 `POSTGRES_USER`、`POSTGRES_PASSWORD`、
`POSTGRES_DB` 一致；字段不会在运行时自动同步，变更后须更新完整 URL。
2026-08-31 已按用户最新本机字段对齐两个 URL，并分别通过 psycopg 3 和
node-postgres 只读连接验证：用户 `postgres`、数据库 `video-maker`、PostgreSQL
16.10。WINDEV-03 先在独立 `_test` 库完成迁移与闭环验证，再经用户授权将
开发库迁移到 `0011_control_plane_recovery` 并创建 Better Auth 表；原生
API/Web 启动与开发库就绪验证通过，没有清理开发数据。
根目录 `.env` 不提交。优先使用 `scripts/dev.ps1` 统一加载根环境并解析存储
路径；手动从 `apps/api` 运行时须显式加载 `uv run --env-file ../../.env ...`，
并自行保证相对存储路径基准一致。

要求：

- 预检脚本只检查端口、凭据、数据库存在、迁移 head，不安装或重启 PostgreSQL。
- 测试必须使用名称以 `_test` 结尾的独立数据库，继续保留现有防误删检查。
- 不自动清空开发数据库，不运行 `dropdb`，不触碰旧 Docker named volume。
- API Alembic 和 Better Auth 迁移都从 Windows 执行。

### 5.4 环境文件分离

- `.env.example`：Windows 原生日常开发默认值。
- `.env.compose.example`：仅保留容器主机名和 Linux 路径。
- `.env`：开发者本机文件，继续忽略提交。
- `STORAGE_ROOT=./data/storage`：使用仓库内已忽略的相对路径，避免硬编码盘符。
- Hatchet Cloud 变量只出现在注释模板中，不提供可用 Token。

### 5.5 Windows 命令入口

新增 `scripts/dev.ps1`，只编排原生命令，不自行安装 WSL、Docker、PostgreSQL 或后台常驻服务：

```powershell
./scripts/dev.ps1 check
./scripts/dev.ps1 migrate
./scripts/dev.ps1 api
./scripts/dev.ps1 web
./scripts/dev.ps1 worker   # 仅 WORKFLOW_BACKEND=hatchet
./scripts/dev.ps1 test
./scripts/dev.ps1 e2e
```

底层命令保持透明，便于单独排错：

```powershell
Set-Location apps/api
uv sync --extra dev
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
uv run pytest -q
uv run ruff check app tests

Set-Location ../..
pnpm install --frozen-lockfile
pnpm --filter @video-factory/web auth:migrate
pnpm --filter @video-factory/web dev
pnpm test
pnpm lint
pnpm typecheck
pnpm build
pnpm test:e2e
```

Playwright 浏览器是 Windows 一次性开发依赖；普通测试不得每次下载或构建浏览器镜像。

### 5.6 Docker 与 Compose 的新边界

- `compose.yaml` 不再是 `dev` 的默认入口。
- 现有开发栈改名或收敛为 `compose.integration.yaml`，调用时必须显式指定文件。
- API、Web 和 Hatchet Worker 的源码热更新不再通过 bind mount 实现。
- CI 可以暂时复用集成 Compose，但不得反向成为 Windows 本地开发前置条件。
- 发布阶段另行提供无 `--reload`、无源码挂载、无 dev dependency 的生产镜像；不在本计划中提前完成部署设计。

## 6. Issue 拆分与实施顺序

每个 Issue 独立验收，前一项通过后再开始依赖它的下一项。

### WINDEV-01：工作流 Backend 配置与生产保护

范围：

- 增加 `WORKFLOW_BACKEND=local|hatchet`。
- 增加 Backend 工厂，消除 `public_api.py` 对 Hatchet 的固定创建。
- Hatchet Client 延迟加载。
- 增加 `production + local` 失败保护和配置测试。

建议文件：`config.py`、`workflow.py`、`public_api.py`、新测试文件。

不做：不实现本地 Runner，不连接 Hatchet Cloud。

验收：没有 Hatchet Token 时，`local` 配置可导入 API；`hatchet` 和生产错误配置给出明确错误。

### WINDEV-02：正式的 Local Workflow Runner

依赖：WINDEV-01。

范围：

- 实现开发专用 `LocalWorkflowStarter/Runner`。
- 复用 `GenerationExecutionService`、稳定幂等键和 Reconciler。
- 接入 FastAPI lifespan 与 `/readyz`。
- 把测试 fixture 改为复用正式实现。

建议文件：新 `local_workflow.py`、`workflow.py`、`main.py`、`tests/conftest.py`、新测试文件。

不做：不实现跨进程 durable sleep，不模拟 Hatchet 服务端。

验收：同一幂等键只执行一次；Mock 和模拟 Provider 均能完成；进程停止不遗留后台任务；生产环境无法启用。

### WINDEV-03：Windows 环境样例、预检和原生命令

依赖：WINDEV-02。

范围：

- 将 `.env.example` 改为 localhost/Windows 默认值，增加 `.env.compose.example`。
- 新增只读 `check` 和原生 `migrate/api/web/test/e2e` PowerShell 入口。
- 将 `Makefile` 默认目标改为原生命令或明确标注为 CI/Unix 兼容入口。
- 更新根 README 的 Windows 快速开始。

不做：不安装 PostgreSQL、Node、Python、FFmpeg 或浏览器。

验收：新检出仓库在依赖已安装的 Windows 上，按 README 可在无 WSL/Docker 情况下启动完整开发栈。

### WINDEV-04：Hatchet Cloud 集成模式

状态：实现与离线验证完成；真实云验收待凭据，尚未整体验收通过。
操作手册见 [Hatchet Cloud Windows 集成](runbooks/HatchetCloud_Windows集成.md)。

依赖：WINDEV-01。

范围：

- 支持 Hatchet Cloud Token、Host、Server URL 和 TLS 配置。
- API 与 Worker 都从 Windows 启动。
- 增加带 marker 的远程集成测试和跳过条件。
- 记录 Cloud 凭据配置与撤销方式。

不做：不部署本地 Hatchet Server，不把云凭据提交到仓库，不把 Cloud 变成单元测试前置条件。

验收：Windows API 能提交一次稳定幂等工作流，Windows Worker 能消费并完成；重复提交复用同一运行。

### WINDEV-05：Compose 隔离与 CI 调整

状态：原生环境隔离改造与本机门禁验证完成；Docker 构建、Compose 联调和远程 CI
尚未执行，列为独立集成验证，不作为本计划的待补验收项。

依赖：WINDEV-03；可与 WINDEV-04 独立实施。

范围：

- 将当前 Compose 明确收敛为集成测试用途。
- 从日常 `dev/test/lint/build/e2e` 目标移除 Compose。
- CI 分成原生语言检查与需要 PostgreSQL／服务编排的集成检查。
- 禁止开发 Dockerfile 使用 `--reload` 或承担本地热更新职责。

不做：不删除发布容器能力，不改变 RunPod Worker 镜像。

原生验收：本地命令不调用 Compose；本机相关门禁通过；CI 配置保留 PostgreSQL 并发、
迁移、FFmpeg、OpenAPI 和 E2E 基线。容器运行及远程 CI 结果单独记录，不以其通过作为
WINDEV-06 的前置条件，也不把静态配置检查记为远程运行通过。

### WINDEV-06：WSL 关闭状态总验收与文档切换

状态：`ACCEPTED`，2026-08-31 原生必选矩阵通过，按需 Cloud 因无凭据明确跳过；
开发总纲、索引和旧维护手册已切换。测试数据仅写入隔离测试库。

依赖：WINDEV-02、03、05 的原生实现与本机验证；WINDEV-04 为按需集成模式，
无云凭据时按第 7 节跳过。Docker/Compose 运行及远程 CI 不作为前置条件。

范围：

- 在 WSL 全部 `Stopped` 的前后状态下执行验收矩阵。
- 更新开发总纲、文档索引和 WSL/Docker 文档：后者降级为集成／发布维护手册。
- 生成验收报告，记录命令、结果、限制和回滚点。

不做：不宣称本地 Backend 具备 Hatchet 的生产耐久保证。

验收：第 7 节所有必选门禁通过，按需项通过或明确记录跳过后，计划状态从
`IN_PROGRESS` 更新为 `ACCEPTED`；该状态仅表示 Windows 原生改造验收通过，
不代表尚未执行的云集成、容器验证或生产发布已通过。

## 7. 验收矩阵

| 门禁 | Windows 原生验证 | 预期 |
|---|---|---|
| WSL 状态 | `wsl.exe --list --verbose` | 开始和结束时所有发行版均为 `Stopped` |
| PostgreSQL | 端口、登录、数据库和 Alembic head 检查 | 使用 Windows 实例，不访问 Compose 主机名 |
| API 启动 | `uv run uvicorn ...` | `/healthz` 与 `/readyz` 返回 200 |
| Web/Auth | `pnpm ... dev` 与登录流程 | Better Auth 使用 Windows PostgreSQL |
| 本地工作流 | 创建 Mock／模拟生成任务 | Outbox 被消费，Job 终态、Output、账本均正确 |
| API 单元测试 | `uv run pytest -q` | 全部通过 |
| PostgreSQL 回归 | 迁移 + 并发账本用例连续 3 次 | 全部通过，无超扣或重复结算 |
| API 静态检查 | `uv run ruff check app tests` | 通过 |
| Web 门禁 | unit、lint、typecheck、build | 全部通过 |
| OpenAPI | 导出并检查 Client diff | 无未提交差异 |
| E2E | Windows Playwright 完整业务闭环 | 通过，且测试过程中不启动 WSL/Docker |
| Hatchet 集成 | Cloud marker 测试 | 显式启用时通过；无凭据时明确跳过 |
| 生产保护 | production + local 配置测试 | 启动失败并给出可诊断错误 |

## 8. 风险与控制

| 风险 | 控制措施 |
|---|---|
| 本地 Runner 与 Hatchet 行为漂移 | 所有业务副作用复用同一 `GenerationExecutionService`；保留 Hatchet Cloud 契约测试 |
| 开发者误把 `local` 当生产耐久工作流 | 命名、日志和文档明确 `development-only`；生产配置硬失败 |
| API 进程退出时本地任务中断 | PostgreSQL 保持真相；Reconciler 重新进入幂等执行路径；不承诺即时恢复 |
| Windows 路径、文件锁或长路径差异 | 存储使用 `pathlib` 和相对路径；加入带空格路径测试；不跨 WSL 文件系统 |
| Windows PostgreSQL 版本或凭据不一致 | 只读预检打印版本和目标数据库；迁移前明确连接目标；测试库必须以 `_test` 结尾 |
| Hatchet Cloud 网络不可用 | 仅影响显式集成门禁，不阻塞日常开发；不自动回退为 local |
| Playwright 首次下载受网络影响 | 浏览器作为一次性前置安装并缓存；普通 E2E 不重复下载 |

## 9. 回滚策略

- 每个 Issue 单独提交，未通过验收不得合并下一项。
- WINDEV-01/02 可通过恢复 `WORKFLOW_BACKEND=hatchet` 回滚，但生产保护不得删除。
- WINDEV-03 的新环境文件和脚本可独立回滚，不修改或删除 Windows PostgreSQL 数据。
- WINDEV-05 在 CI 失败时可暂时恢复显式 `compose.integration.yaml` 路径，不恢复本地开发对 Compose 的默认依赖。
- 任何回滚都不得执行 `docker compose down -v`、删除 Windows 数据库或清空本地存储。

## 10. 本计划明确不做

- 不让 WSL 常驻，也不通过登录任务自动启动 WSL 或 Docker。
- 不在 Windows 上非官方移植 Hatchet Embedded sidecar。
- 不删除 Docker、发布镜像或 RunPod／ComfyUI 镜像能力。
- 不改变 Job 状态机、Outbox、账本、结算、取消、重试和媒体校验语义。
- 不顺带实现真实 RunPod、GPU、对象存储、支付或生产部署。
- 不把 Hatchet Cloud、网络或任何外部服务设为日常单元测试前置条件。

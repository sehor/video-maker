# 测试约定

## 计划一业务修复验收（2026-09-07）

- 全量入口 `pnpm test:all`：首轮 Python 410 passed、1 failed、5 errors、1 deselected。
  修复测试注入方式与 Windows 清表超时后，定向重跑并按 JUnit 用例 ID 核对：416 个选中用例
  均已有通过记录。此为分次合并验证，不表示单次全量命令全绿。
- Web 26 passed；模拟 I2V E2E 2 passed（22.1s），Nuxt 生产构建通过。浏览器覆盖绑定失败
  重试、刷新保留首帧、接单响应丢失恢复、一次轮询错误及下载失败重试、固定样本加载。
- Ruff、前端 lint、TypeScript、OpenAPI/客户端一致性均通过；就绪检查要求 0015。
- 本轮未升级开发库、未启用快照必填、未验证真实 GPU/云服务。详细证据和提交见
  [修复验收报告](reports/2026-09-07_业务逻辑修复验收.md)。

## 日常约定

按用户要求，日常开发默认使用轻量测试，不自动运行数据库集成、迁移、并发压力或浏览器测试。
外部服务使用模拟；数据库原有用例保留为手动验证，不改变业务接口和账本规则。

## 执行入口

| 命令 | 验证范围 | 依赖 |
|---|---|---|
| `pnpm test` | 无数据库 Python 测试 + Web 单元测试，顺序执行 | Python、pnpm；无需 PostgreSQL |
| `pnpm test:python` / `pnpm test:unit` | 无数据库的逻辑、元数据、Storage、脚本 | Python |
| `pnpm test:db` | 仅 4 条核心流程：项目 CRUD、越权、上传下载、Mock 生成输出 | 手动；Windows PostgreSQL |
| `pnpm test:db:full` | 全部数据库业务、迁移、约束、幂等和并发回归 | 手动；Windows PostgreSQL |
| `pnpm test:media` | 不依赖数据库的元数据检查 | Python；禁止媒体进程 |
| `pnpm test:web` | 前端任务状态逻辑 | pnpm |
| `pnpm test:e2e` | 浏览器业务闭环，使用固定媒体样本 | 手动；PostgreSQL、Chromium |
| `pnpm test:all` | 全部 Python、Web 与 E2E | 手动；全部本机依赖 |

普通 Python 定向测试：`uv run --project apps/api --no-sync python scripts/test.py python -k keyword`。
只有明确涉及数据库行为时才启用，例如：
`uv run --project apps/api --no-sync python scripts/test.py python apps/api/tests/test_migrations.py --run-db`。
直接执行 pytest 也默认排除 `database` 用例；必须显式传 `--run-db`。
只选择数据库用例而未启用时，不会执行测试，也不会计为通过。
根 `pytest.ini` 是唯一收集配置；`dev.ps1 test/test-api/test-web/e2e` 保留为对应入口的别名。

日常只跑与改动相关的轻量用例；普通业务联调需要数据库时先用 4 条核心流程。
只有修改迁移、约束、事务或并发逻辑时，才选择相关数据库测试；不再因媒体/文档/类型改动运行整套数据库回归。
完整数据库和 E2E 在发布前或明确要求时执行，不并行启动多组重测试。

## 数据隔离

- 只读取 `.env` 或 shell 的 `TEST_DATABASE_URL`，必须是 loopback PostgreSQL、库名以 `_test` 结尾，
  且不能与开发库同名。禁止通过 query 参数覆盖连接目标。缺配置失败，不回退 SQLite。
- 本机复用已有 Windows PostgreSQL；不自动安装、启动、创建或删除本机数据库。
- 每次运行创建随机 `test_<uuid>` schema。普通集成测试执行一次 Alembic 到 head，
  测试间只 TRUNCATE 本次 schema 的数据，保留真实事务提交、多个连接、触发器及行锁。
  测试连接单独设置 `synchronous_commit=off`，避免每次写入等待磁盘同步；不修改实例配置。
  这不验证数据库断电恢复能力，提交可见性、延迟约束和事务原子性仍由 PostgreSQL 实际执行。
- 迁移升级/降级和 E2E 各用新的 schema；清理只针对本次成功创建的 schema。
  不删除 public、其他 schema、既有表或用户数据。
- 不支持在同一 pytest 进程内用 xdist 共享 fixture；独立进程可以使用同一测试库的不同 schema。

## 模拟与真实验证的边界

- RunPod 使用 `httpx.MockTransport`；Hatchet 使用 SDK 接口 fake；发布命令用注入的 runner 记录
  参数和失败，不运行镜像。测试拦截意外的 WSL/Docker、FFmpeg/ffprobe 命令和外网 socket。
- `database` marker 标识数据库测试，`--run-db` 才启用执行与隔离。无 marker 的测试使用共享业务 engine 时立即失败。
- `client` 是业务便捷 fixture：提交生成后同步驱动 local outbox 并等待任务结束；
  `raw_client` 不代为驱动。后台集成测试真实使用 API lifespan 的 dispatcher，证明自动调度。
- `media` 测试验证 Provider 元数据、对象基础检查与结果接收；自动测试禁止启动 FFmpeg/ffprobe。
  不逐帧解码，不把声明合法标记为“已证明可播放”；E2E 继续检查浏览器实际加载固定样本。
  Mock 视频样本已由旧 MPEG-4 Part 2 重建为 H.264、2 秒、25 fps，且与 Mock 元数据一致。
- 真实 Cloud 是显式 `dev.ps1 test-hatchet` 验证，不属于默认测试；缺凭据必须失败。
  默认 deselect live 和当前 OS 不适用的用例，报告明确显示数量，不计为通过。
- 运行期 skip、xfail、xpass 均导致非零退出；没有收集到测试也是失败。
  Playwright 禁用重试并禁止 `test.only`。外部真实 GPU、Cloud 和镜像未运行时不得声称已验证。

## 测试维护

- 验证可观察行为和负例，不固定历史 Issue 状态、旧依赖版本或 CI 的整段命令文本。
- 生成的 API 类型固定 LF 换行，避免 Windows CRLF checkout 被生成器误判为陈旧文件。
- 账本数据库测试必须验证不可修改、延迟平衡约束和失败回滚，不能只检查触发器名称。
- 并发测试使用起跑屏障、独立请求和真实 PostgreSQL 锁，不通过循环重跑把偶发通过当验收。
- `.test-runs/<group>-*/` 保留本轮 JUnit、临时文件和服务器日志；浏览器保留失败 trace。
  自动清理数据库 schema 和受管服务，运行产物忽略提交；不扫描或删除其他旧目录。
- CI 的 PR/push 默认只跑轻量测试和静态检查；手动运行时勾选 `full_integration` 才创建临时数据库并执行完整数据库/E2E。远程 CI 需要推送后实际执行，
  本机通过不能代替远程 CI 运行结果。

## 验收标准

1. 默认入口覆盖无数据库的 Python/仓库脚本/Web，定向入口只执行所选层。
2. 显式启用数据库测试时，只验收选定用例；迁移/约束/并发仍使用 Windows PostgreSQL，无 SQLite 替代。
3. 外部操作不启动 WSL/Docker；缺依赖、跳过和测试失败不能返回成功。
4. 显式执行 E2E 时使用独立数据和临时服务，不靠重试。
5. 相关静态检查通过，记录实际结果、局限并本地提交。

## 之前 FFmpeg 验收移除验证（2026-09-06，非本次运行）

- 元数据、RunPod 适配器、Worker 契约及脚本定向回归：81 passed，8.91s。
- Windows PostgreSQL 结果接收与后台工作流：11 passed，46.56s；一条 pytest 缓存目录权限警告不影响测试结果。
- 最后补充的媒体进程拦截与契约检查：7 passed，18 deselected，1.17s（与前组有重叠，不相加）。
- 相关 Python Ruff 检查、Git 差异检查通过；未运行 FFmpeg/ffprobe、WSL/Docker、真实 GPU 或前端构建。
- 此次历史改造仍保留大小/SHA-256 一致性校验；后续取消规则见下方媒体哈希移除记录。

## 之前测试重构时的验证（2026-09-06，非本次运行）

| 检查 | 结果 |
|---|---|
| `pnpm test` | Python 312 passed，2 deselected（Cloud / POSIX）；Web 14 passed |
| `pnpm test:unit`，故意设置不可用数据库 URL | 180 passed，19.04 秒；未访问数据库 |
| `pnpm test:e2e` | 2 passed，13.4 秒（不含原生迁移和构建） |
| Ruff / 前端 lint / TypeScript | 通过 |
| OpenAPI 导出、API Client `--check` | 通过；接口内容无变化，修正 Windows 换行误报 |

完整 Python 回归本轮为 459.73 秒，同时进行了 E2E 构建及类型检查；这不是独占机器的性能基准。
快速开发可使用分层入口，完整回归仍保留真实 PostgreSQL、媒体和并发检查。
本次没有执行 WSL/Docker、真实 Cloud/GPU 或远程 GitHub Actions；远程 CI 配置需推送后验证。


## 之前媒体哈希与声明大小一致性检查移除（2026-09-06，非本次运行）

- 图片上传、生成视频存储、stat 和结果接收不再计算/比对媒体 SHA-256，不比对 Provider 声明大小；实际字节数仍用于大小上限与记录。
- 保留权限、受控对象键、文件存在、非空、大小上限和基础元数据检查；自动测试继续禁止 FFmpeg/ffprobe 及现场编码。
- 旧 sha256 值保留，新媒体返回 null；Worker 可省略旧 size_bytes/sha256 字段。OpenAPI 与 TypeScript 客户端同步生成。
- 0012 迁移兼容历史记录，已在独立测试库验证升级及有空值时拒绝降级；本地开发库已迁移，原生开发检查通过。
- Ruff、前端 lint/typecheck、生成客户端一致性和 Git 差异检查通过。
- 完整回归 `pnpm test`：Python **342 passed，1 deselected**（386.75 秒），前端 **14 passed**；包含 Windows PostgreSQL 的结果接收、工作流及迁移回归。
- 本轮未运行 E2E、真实 GPU、WSL/Docker 或媒体编码/解码进程。


## 日常数据库测试简化验证（2026-09-06）

- 将 DATABASE_URL 和 TEST_DATABASE_URL 都设为不可用地址，执行新的 `pnpm test`：Python **231 passed，121 deselected，15.84 秒**；Web **14 passed，1.38 秒**。
- 默认排除数据库/live/平台不适用用例；不把排除项计为通过。选择规则回归以临时假用例验证 `--run-db` 的开关行为，不连接数据库。
- `test:db` 的 4 个目标已检查存在，完整入口仍保留所有数据库用例；本次没有执行数据库集成、迁移、E2E 或媒体进程。
- Ruff、Git 差异检查、package/CI 配置语法与 CI 集成步骤的手动条件检查通过。远程 CI 尚未运行。

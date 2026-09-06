# 测试约定

本次范围来自用户要求：重构整个测试体系，使用 Windows 数据库，不访问 WSL；
外部基础设施用模拟替代。覆盖测试入口、fixture、数据库迁移、断言、浏览器流程和 CI，
不改变业务接口、账本规则或生产部署。当前开放 Issues #14–#16 是阶段三业务任务，
本次不扩展其真实 GPU/RunPod 工作。

## 执行入口

| 命令 | 验证范围 | 依赖 |
|---|---|---|
| `pnpm test` | 所有 Python + Web 单元测试 | Windows PostgreSQL；无需媒体二进制 |
| `pnpm test:unit` | 配置、Provider 协议、调度逻辑、Storage、脚本 | Python；不访问数据库 |
| `pnpm test:db` | 真实迁移、约束、业务、幂等、并发 | Windows PostgreSQL |
| `pnpm test:media` | Provider 元数据、对象大小/SHA-256、结果接收 | Python；部分用 PostgreSQL，不启动媒体进程 |
| `pnpm test:web` | 前端任务状态逻辑 | pnpm |
| `pnpm test:e2e` | 注册、权限跳转、项目/镜头/生成、固定视频样本加载及刷新 | PostgreSQL、Chromium；不现场编码 |
| `pnpm test:all` | 上述完整测试 | 全部本机依赖 |

Python 定向测试：`uv run --project apps/api --no-sync python scripts/test.py python -k keyword`。
根 `pytest.ini` 是唯一收集配置，包含 `apps/api/tests` 和根 `tests`，不再单独跑 unittest。
`dev.ps1 test/test-api/test-web/e2e` 保留为统一入口的别名。

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
- `database` marker 显式启用数据库隔离。无 marker 的测试使用共享业务 engine 时立即失败。
- `client` 是业务便捷 fixture：提交生成后同步驱动 local outbox 并等待任务结束；
  `raw_client` 不代为驱动。后台集成测试真实使用 API lifespan 的 dispatcher，证明自动调度。
- `media` 测试验证 Provider 元数据、对象完整性与结果接收；自动测试禁止启动 FFmpeg/ffprobe。
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
- CI 使用 Windows 临时 runner 数据库，与本机入口一致。改造后的远程 CI 需要推送后实际执行，
  本机通过不能代替远程 CI 运行结果。

## 验收标准

1. 默认入口覆盖 Python/仓库脚本/Web，定向入口只执行所选层。
2. Windows PostgreSQL 真实迁移、约束、并发和业务回归通过，无 SQLite 替代。
3. 外部操作不启动 WSL/Docker；缺依赖、跳过和测试失败不能返回成功。
4. E2E 使用独立数据和临时服务，验证真实 UI/媒体，不靠重试。
5. 相关静态检查通过，记录实际结果、局限并本地提交。

## FFmpeg 验收移除验证（2026-09-06）

- 元数据、RunPod 适配器、Worker 契约及脚本定向回归：81 passed，8.91s。
- Windows PostgreSQL 结果接收与后台工作流：11 passed，46.56s；一条 pytest 缓存目录权限警告不影响测试结果。
- 最后补充的媒体进程拦截与契约检查：7 passed，18 deselected，1.17s（与前组有重叠，不相加）。
- 相关 Python Ruff 检查、Git 差异检查通过；未运行 FFmpeg/ffprobe、WSL/Docker、真实 GPU 或前端构建。
- 控制面和 POC 接收均只检查元数据；现有下载、SHA-256 校验和发布的文件 I/O 仍然存在，本次未声称消除这部分成本。

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

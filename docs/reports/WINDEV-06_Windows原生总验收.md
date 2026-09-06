# WINDEV-06｜Windows 原生总验收

日期：2026-08-31。分支：`codex/windows-native-development`；前置提交：`f15662b`。
工作项：WINDEV-06，仅做原生总验收和开发文档切换，不更改业务实现、接口、数据库模型或依赖。

状态：`ACCEPTED`。所有原生必选门禁通过，按需项明确跳过；本次 Windows 原生改造无剩余必做项。
不代表真实 Cloud、容器集成或生产发布已通过。

## 范围与环境

- Windows 原生运行 API、Web、PostgreSQL、FFmpeg 和 Playwright；默认 `WORKFLOW_BACKEND=local`。
- 开始和结束时 `wsl.exe --list --verbose` 均显示唯一发行版 `Ubuntu-22.04` 为 `Stopped`，版本 2。
  该命令只查询状态；本轮不执行 `wsl -d`、Docker 构建、Compose 或远程 CI。
- 实测运行时：Python 3.13.7、uv 0.11.19、Node 22.16.0、pnpm 9.12.3、PostgreSQL 16.10、
  FFmpeg/ffprobe `8.0.1-essentials_build-www.gyan.dev`。未升级依赖或更改锁文件。
- 开发库 `video-maker` 只做只读预检；URL 格式、API/Auth 目标与配置字段一致，
  Alembic 为 `0011_control_plane_recovery`，Better Auth 五张表齐全。
- API 回归使用已有 `video-maker_windev03_test`，测试 fixture 重建其中业务表。
  E2E 使用另一已有库 `video-maker_windev03_e2e_test`，迁移和合成账号、项目、镜头均局限于该库；
  不创建或删除数据库，不修改开发库、根 `.env`、密码或旧容器数据。

## 本轮验收矩阵

| 门禁 | 实际执行与结果 |
|---|---|
| WSL 前后状态 | 开始和结束均为 `Stopped`，全程未调用发行版启动命令 |
| Windows PostgreSQL 预检 | `scripts/dev.ps1 check` 通过；两个客户端连接与迁移 head 正确 |
| 原生迁移 | 对 E2E 隔离库执行原生 `migrate`；Alembic head 不变，Auth 返回无需迁移；迁移前后检查通过 |
| API 启动 | 经 `scripts/dev.ps1 api` 启动；`/healthz`、`/readyz` 均为 HTTP 200 |
| Web/Auth | 经 `scripts/dev.ps1 web` 启动；`/login` HTTP 200，真实浏览器完成注册与登录 |
| Mock／模拟 Provider | 全量回归通过；后台 Outbox、Local Runner、Output、单次结算、带空格存储路径和 Reconciler 恢复均覆盖 |
| API 全量回归 | Windows + 独立 PostgreSQL：266 通过、3 跳过，853.14 秒（14 分 13 秒） |
| 并发账本连续三轮 | 单镜头提交、Batch 提交各一例；每轮 2 项通过，分别 4.71、5.00、7.25 秒 |
| API 静态检查 | 原生 `lint` 的 Ruff 检查通过 |
| Web 门禁 | unit 1 项通过；lint、typecheck、build 全部通过 |
| OpenAPI Client | 重新导出 OpenAPI，Client `--check` 通过；`packages/api-client` 无 diff |
| E2E | 1 项通过（13.5 秒），注册 → 项目 → 镜头 → 测试额度 → 生成成功 → 视频可见 |
| 环境边界及仓库回归 | 根目录 unittest 32 项通过；仓库基线检查通过 |
| Hatchet Cloud（按需） | `scripts/dev.ps1 test-hatchet` 明确输出 SKIP：未配置开发租户 Token；没有远程调用 |
| 生产保护 | `production + local` 拒绝启动、Runner 启动时重检保护均通过 |

API 的 3 项跳过分别为 Hatchet Cloud、需显式启用的 Hatchet 服务端集成，以及 Windows
不适用的 POSIX 进程组测试；不把跳过当作通过。结束时再次执行开发库只读 `check` 通过，
目标与 head 未变。文档相对链接、代码围栏及 `git diff --check` 均通过。

本轮 E2E 使用现有 Playwright 用例和已安装 Chromium，未安装浏览器或切换测试框架。
API/Web 在独立子进程环境中加载测试库 URL 和独立存储路径，不把凭据写入临时配置文件。
启动前检查 8000/3000 空闲；结束后仅停止本轮持有 PID 的进程树，不停止用户服务。

## 复核命令

从仓库根目录运行，前置依赖已安装；API 回归用独立 `_test` 库，不能与 E2E 共用正在重建的库。

```powershell
./scripts/dev.ps1 check
./scripts/dev.ps1 test-api --durations=10
uv run --project apps/api --no-sync python -m unittest discover -s tests
./scripts/dev.ps1 test-web
./scripts/dev.ps1 lint
./scripts/dev.ps1 typecheck
./scripts/dev.ps1 build
./scripts/dev.ps1 generate-client
pnpm --filter @video-factory/api-client check
git diff --exit-code -- packages/api-client
./scripts/dev.ps1 test-hatchet
uv run --project apps/api --no-sync python scripts/check_repository_baseline.py
```

并发检查使用以下命令顺序执行三次，每次确认目标为 `video-maker_windev03_test`：

```powershell
./scripts/dev.ps1 test-api tests/test_quote_ledger.py::test_competing_submissions_cannot_overdraw tests/test_batch_atomic.py::test_competing_batches_cannot_overdraw_wallet
```

本轮 OpenAPI 导出实际通过 `apps/api` 下的 `uv run --no-sync python scripts/export_openapi.py`
执行；该命令也是 `generate-client` 的导出步骤。同目录运行
`uv run --no-sync python scripts/export_ffmpeg_build.py`，确认本机 FFmpeg 的 GPL 标记、
H.264 解码器和 libx264 编码器可用。

E2E 的可复核方式：按根 README 准备已忽略的 `.env.local`，使 API/Auth URL、
`POSTGRES_DB` 指向专用 E2E 测试库，保持本机凭据、独立 `STORAGE_ROOT` 和 `local` Backend；
对 `migrate`、`api`、`web`、`e2e` 均传入 `-EnvFile .env.local`。API/Web 在两个终端运行，
E2E 完成后退出自己的进程。本轮用临时 Python 驱动传入等价的子进程环境，并自动关闭自身服务；
临时驱动不作为产品脚本提交。

## 文档切换

- 开发总纲明确 Windows 原生默认路径、Local Backend 的职责和生产禁用限制。
- 文档索引与根 README 指向原生入口，补充 E2E 数据隔离说明。
- 旧 WSL/Docker 文档降级为独立集成／发布维护手册；本地数据库任务先分流到原生只读预检。
  旧环境版本和验收标为历史记录；容器示例显式指定 `.env.compose` 与 `integration` profile，
  不加载本机原生 `.env`，保留旧卷，不自动清理。
- 计划状态仅依据原生必选矩阵与按需项的明确处理更新；不把未执行的容器或云测试记为通过。

## 限制与回滚

- 真实 Hatchet Cloud 未验收；需要有效开发租户 Token 后单独显式验证，不阻塞原生日常开发。
- Docker 构建、Compose、远程 GitHub CI 和生产发布均未执行，属于独立验证，不是本次剩余任务。
- 原生 Local Backend 只保证开发闭环，不能替代生产 Hatchet 的跨进程耐久编排。
- 本轮复用已安装依赖及已有测试库；未重新验证全新机器安装，也未重建空 PostgreSQL 库。
  空库迁移与升级链的回归由 API 测试中的临时 SQLite 库覆盖；本机 PostgreSQL 本轮验证已有 head 上重跑迁移。
- Web 构建仍有已有的 Nuxt 插件耗时提示和 Node `DEP0155` 警告，不影响构建通过；没有顺带升级依赖。
- 沙箱初次限制了 WSL 状态查询和 pnpm 用户目录读取；相同原生检查在获准权限下重跑通过，
  不是数据库认证失败，没有为此更改凭据。
- 本轮仅文档修改。需要回滚时，以 `f15662b` 为前置参考建立新的 revert 提交；不硬重置工作区、
  不执行 `down -v`、不删除数据库或本地数据。测试账号与结果保留在独立测试库及忽略提交的临时目录。

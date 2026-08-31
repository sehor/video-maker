# WINDEV-05 Compose 隔离与 CI 验证

日期：2026-08-31。分支：`codex/windows-native-development`；前置提交：`b286f96`。
范围为 WINDEV-05，不改变业务接口、数据库模型或 RunPod Worker 镜像，不执行 WINDEV-06 文档总切换。

状态：配置改造与本机验证完成。**没有执行 Docker 镜像构建或远程 GitHub CI**；
不将静态检查、Windows 本机 E2E 或上一阶段结果记为容器集成通过。

验收边界澄清（2026-08-31）：本阶段的 Windows 原生交付是将 Compose 从默认开发命令中
隔离，并验证本机开发门禁。Docker 构建、Compose 联调和远程 CI 属于独立集成／发布验证，
不列为 Windows 原生改造的剩余必做项，不要求为此启动本机 WSL/Docker。

## 改动与边界

- Compose 所有服务增加 `integration` profile；Makefile 的容器入口明确传入 `.env.compose`
  和 profile。原生 `dev/test/lint/build/e2e` 等入口继续调用 PowerShell，不进入 Compose。
  未开启 profile 时不启动服务；显式点名服务仍可能激活相关 profile，不把 profile 当权限隔离。
  行为依据 [Compose profile 文档](https://docs.docker.com/compose/how-tos/profiles/)。
- 移除 API/Worker/Web 的源码和 node_modules 挂载，API 不使用 `--reload`，Web 镜像构建时
  生成 Nitro 产物，启动时运行 `node .output/server/index.mjs`。API/Web 增加就绪探针，
  Worker 等 API 迁移与就绪后启动；API/Web 端口仅绑定 127.0.0.1。
- API 镜像改为根目录构建上下文，使用 uv 0.11.19 和现有 `uv.lock` 的 `--locked` 安装。
  补全 `.dockerignore`，排除本机 `.env`、虚拟环境、缓存和数据。选用 uv 的 MIT 许可通知，
  将上游原文复制到镜像 `/usr/share/doc/uv/copyright`，登记工具版本；未更改 Python/JS 锁文件。
  安装方式参照 [uv Docker 集成](https://docs.astral.sh/uv/guides/integration/docker/)和
  [GitHub Actions 集成](https://docs.astral.sh/uv/guides/integration/github/)。
- 保留已有 PostgreSQL/Hatchet/Storage 卷名，不重命名本地 Compose 项目，不删除现有卷。
  `web-node-modules` 旧卷仅停止挂载，不自动删除。本 Compose 仍是集成配置，不是生产部署模板。
- Hatchet 幂等集成用例增加 120 秒等待上限；未配置服务时仍跳过，启用后超时应失败。
  顺带修正原生 lint 实际发现的 `scripts/dev.py` 单行长度问题，没有改变命令行为。

## CI 门禁映射

| 任务 | 验证内容 | 外部服务 |
|---|---|---|
| `native-checks` | uv/pnpm 锁定安装、API SQLite、Ruff、环境边界、固定 Worker 合同、FFmpeg、Web unit/lint/typecheck/build、OpenAPI Client diff | 无 PostgreSQL/Hatchet/Compose；依赖安装需网络 |
| `integration` | 镜像构建、PostgreSQL 空库迁移与唯一 head、三轮并发账本、容器 FFmpeg 清单、Hatchet 重复启动复用、构建 Web E2E | CI 自己创建的 Compose 服务 |
| `test` | 必须同时满足前两项成功，失败/取消/跳过均不能通过 | 无；保留原必需状态名 |

CI 显式使用 `.env.compose.example` 和 `video-maker-ci-<run_id>-<attempt>` 项目，
不会复用本机数据库卷或 Cloud 凭据。清理带项目名前缀检查，只删除本次临时项目。
失败时保留服务状态、有限服务日志和 Playwright 诊断 3 天，不上传 Hatchet Token volume，
也不输出完整 Compose 展开配置。CI 平台执行本身仍待后续推送或 PR 触发，本次只提交本地。
环境文件与 profile 选择方式见 [Compose 环境变量文档](https://docs.docker.com/compose/how-tos/environment-variables/envvars/)。

## 本机实际结果

| 检查 | 结果 |
|---|---|
| 根目录 unittest | 32 passed，包含 8 项新增环境边界检查 |
| actionlint | v1.7.12 通过；下载后核对官方 SHA256，再执行 |
| 官方 Compose JSON Schema | 通过；同时检查 profile、依赖无环、命名卷和构建上下文 |
| uv 锁文件离线预检 | `sync --locked --extra dev --dry-run --offline` 通过，解析 66 个包；未实际安装或改锁 |
| API SQLite 全量回归 | 266 passed、3 skipped，2193.83 秒（36 分 33 秒） |
| PostgreSQL 并发账本 | 3 轮均 2 passed，分别 7.82 / 8.07 / 9.10 秒 |
| API / Web lint | 通过；API 命令从与 CI 相同的 `apps/api` 目录执行 |
| Web unit / typecheck / build | 1 passed / 通过 / 通过；构建有既有插件耗时和 DEP0155 警告 |
| OpenAPI | 重新导出、Client `--check` 通过，`packages/api-client` 无差异 |
| FFmpeg 实际清单 | 导出通过：8.0.1-essentials_build-www.gyan.dev，GPL，具备 H.264 编解码器 |
| 构建 Web + 原生 API E2E | 1 passed（7.4 秒）；就绪探针通过，注册到生成 MP4 闭环通过 |
| 仓库命令基线与差异检查 | 通过 |
| WSL 前后复查 | Ubuntu-22.04 均为 Stopped；未启动发行版或 Docker |
| Docker build / Compose 完整运行 / GitHub Actions | 未执行，不宣称通过 |

并发回归复用独立 `video-maker_windev03_test`；E2E 使用已迁移的
`video-maker_windev03_e2e_test`，临时 API 为 Local Backend，Web 直接运行生产构建产物。
E2E 新增合成账号和业务数据仅在该测试库；测试结束已停止本次 API/Web 进程。
没有改动开发库、根 `.env`、本机密码、数据库持久化参数或旧 Docker 数据。

复核命令：

```powershell
uv run --project apps/api --no-sync python -m unittest discover -s tests
uv run --project apps/api --no-sync python scripts/check_repository_baseline.py
./scripts/dev.ps1 test-web
./scripts/dev.ps1 lint
./scripts/dev.ps1 typecheck
./scripts/dev.ps1 build
./scripts/dev.ps1 test-api tests/test_quote_ledger.py::test_competing_submissions_cannot_overdraw tests/test_batch_atomic.py::test_competing_batches_cannot_overdraw_wallet
```

API SQLite 回归进程未设置 `TEST_DATABASE_URL`，由 conftest 强制选择 SQLite 和 Local Backend，
通过独立 pytest 临时目录运行，不使用根 `.env` 中的开发数据库连接。
actionlint 官方校验和为
`6e7241b51e6817ea6a047693d8e6fed13b31819c9a0dd6c5a726e1592d22f6e9`。
schema 校验器和 actionlint 仅放在忽略提交的临时环境，未加入项目依赖。

## 独立集成验证与回滚

本机没有 Windows Docker CLI，WSL 已停止，因此未为本轮验证启动 Docker，也未操作旧容器。
后续若验收容器集成能力，需要在对应环境实际确认 Linux 镜像构建、Hatchet 服务端与锁定
SDK 的运行兼容性，以及容器内 E2E；Windows 构建产物 E2E 不能替代这些验证。
这些未执行项不阻塞 Windows 原生改造验收。WINDEV-04 的真实 Cloud 验收仍待开发 Token，
无凭据时按原生矩阵明确跳过；当前原生必做项为 WINDEV-06 的整体矩阵和维护文档切换，
完成前计划保持 `IN_PROGRESS`。

回滚以 `b286f96` 为前置参考，使用新的 revert 提交；不执行硬重置、清库或删除已有 volume。
日常开发继续使用 Local Backend 和原生命令，不受容器集成是否运行影响。

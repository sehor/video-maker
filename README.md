# AI 视频镜头工厂

仓库使用 pnpm 9.12.3 和根目录唯一的 `pnpm-lock.yaml`。常用前端命令：

```bash
pnpm install --frozen-lockfile
pnpm test
pnpm lint
pnpm typecheck
pnpm build
pnpm test:e2e
```

Python API 依赖与命令由 uv 管理；Compose 工作流可通过 Makefile 的同名目标执行。

## SIM-05 模拟验收状态

SIM-05 已完成并标记为 `SIMULATION_ACCEPTED`：Submit、Poll、Webhook、Retry、Cancel、
Finalize、Ledger、Project、Storage、Dead Letter 和 Readiness 的故障矩阵已使用 Fake Clock、
模拟 Provider／Storage、Fault Injector 与媒体 fixture 验收。PostgreSQL 空库迁移、并发账本
回归连续 3 轮和 Playwright 完整业务闭环均已实际通过。

该状态不代表真实 RunPod、GPU、网络、对象存储、视频质量、性能或成本已经验证。
真实 RunPod／worker-comfyui 路线仍为 `CONDITIONAL`／默认禁用。

- [SIM-05 模拟故障注入验收报告](docs/reports/SIM-05_模拟故障注入验收报告.md)
- [模拟控制面故障恢复 Runbook](docs/runbooks/模拟控制面故障恢复.md)

本地 API 验证命令：

```bash
cd apps/api
uv run --extra dev pytest -q -m "not integration"
uv run --extra dev ruff check app tests
```

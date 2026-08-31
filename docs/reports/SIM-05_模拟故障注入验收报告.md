# SIM-05 模拟故障注入验收报告

> 日期：2026-08-30  
> 当前状态：`SIMULATION_ACCEPTED`  
> 真实路线：`CONDITIONAL`／默认禁用

## 1. 验收边界

本报告只证明离线模拟控制面的事务、幂等、恢复、媒体校验和生命周期不变量。
测试使用 Fake Clock、`DeterministicRunPodSimulator`、`FaultInjector`、模拟 Provider、
模拟 Storage 和本地媒体 fixture；未申请或使用真实 RunPod 凭据，也未提交付费任务。

明确未验证：真实 GPU、真实 RunPod API、真实网络及其抖动、真实对象存储、真实 Hatchet
服务、真实视频质量、真实冷启动／运行性能、真实供应商成本和生产容量。

## 2. 故障矩阵与回归证据

| 领域 | 场景 | 自动化证据 | 结论 |
|---|---|---|---|
| Submit | 响应丢失、结果未知、重复提交、保存 Provider Job ID 前后崩溃 | `test_provider_contract.py::test_submit_unknown_reconciles_without_duplicate_submit`；`test_provider_polling.py::test_submit_outcome_unknown_is_reconciled_after_restart_without_resubmit`；`test_transactional_outbox.py::test_dispatcher_recovers_crash_windows_after_lease_expiry` | 通过 |
| Poll | 无 Webhook、进程重启、轮询预算耗尽 | `test_provider_polling.py::test_queued_job_recovers_from_database_without_duplicate_submit_or_settlement`；`test_poll_budget_timeout_finishes_after_bounded_attempt_retries` | 通过 |
| Webhook | 重复、同 ID 不同 payload、租约过期、Poll 竞争、迟到旧 Attempt | `test_provider_webhooks.py::test_webhook_requires_valid_signature_and_deduplicates_completion`；`test_stale_webhook_claim_is_reconciled_after_lease_expiry`；`test_poll_and_webhook_race_produces_one_final_result`；`test_late_old_attempt_event_cannot_rewrite_success` | 通过 |
| Output | 正确 MP4、损坏媒体、错误容器／codec／尺寸／时长、缺失、SHA 不一致 | `test_media_validation.py`；`test_remote_artifacts.py::test_valid_remote_artifact_publishes_and_settles_once`；`test_invalid_remote_artifact_fails_refunds_and_never_publishes` | 通过 |
| Finalize | 并发完成、Commit 后重放、唯一最终 Output | `test_provider_webhooks.py::test_poll_and_webhook_race_produces_one_final_result`；`test_stage_three_simulation_acceptance.py::test_terminal_matrix_survives_restart_and_replay_without_duplicate_effects` | 通过 |
| Retry | 临时失败后成功、到达 Attempt 上限、旧 Attempt 迟到成功 | `test_provider_webhooks.py::test_late_old_attempt_event_cannot_rewrite_success`；`test_provider_polling.py::test_poll_budget_timeout_finishes_after_bounded_attempt_retries` | 通过 |
| Cancel | Commit 后崩溃、调用前后崩溃、响应丢失、临时失败、成功竞态、重复取消 | `test_provider_cancel_outbox.py` 全部场景；`test_stage_three_simulation_acceptance.py::test_cancel_and_replay_release_once_without_publishing` | 通过 |
| Ledger | 并发防超扣、Settle／Release 重放与互斥、Batch 部分成功、迟到事件 | `test_quote_ledger.py`；`test_batch_atomic.py`；`test_job_state_idempotency.py::test_stale_terminal_competitors_only_allow_one_winner` | 通过 |
| Project | 活动任务拒删、重复软删除、删除后禁止写入、历史保留 | `test_project_cleanup.py::test_project_delete_is_soft_idempotent_and_preserves_audit_history`；`test_project_with_active_job_cannot_be_deleted`；`test_deleted_project_rejects_new_resources` | 通过 |
| Storage | 删除临时失败、部分成功、对象缺失、租约过期重领 | `test_project_cleanup.py::test_storage_cleanup_retries_partial_failure_and_records_each_object`；`test_cleanup_reclaims_expired_lease_after_delete_crash`；`test_runpod_simulator.py::test_fake_remote_storage_clock_expiry_and_faults_are_controllable` | 通过 |
| Dead Letter | 永久失败、查询、审计、显式重放、重复重放 | `test_control_plane.py::test_outbox_dead_letter_is_queryable_audited_and_replayable` | 通过 |
| Readiness | DB／迁移／Storage／工作流不可用与恢复 | `test_control_plane.py::test_readiness_distinguishes_dependencies_and_migration_head`；`test_health_is_live_when_readiness_fails` | 通过 |

所有时间推进均由 Fake Clock 或明确的短时并发同步控制；模拟生命周期测试不依赖真实长时间
等待。`test_media_validation.py` 中的 `sleep` 只存在于 ffprobe 子进程超时／终止测试，不用于
模拟 Provider、Outbox 或 Job 生命周期推进。

## 3. 核心不变量

- 每个 Job 最多发布一个最终 Output；
- 每个 Job 的 `SETTLE` 与 `RELEASE` 互斥，且各最多一次；
- 重放、租约过期重领、进程重启和重复事件不产生第二个业务结果；
- Retry 创建新 Attempt，旧 Attempt 的迟到事件不能改写新终态；
- Project 删除保留 Job、Attempt、Event、Output 与账本审计历史；
- 永久失败事件可查询、可审计、可显式幂等重放；
- 未实现或未真实验证的路线不能启用或售卖。

## 4. 实际门禁记录

| 门禁 | 命令 | 结果 |
|---|---|---|
| 仓库命令基线 | `uv run python scripts/check_repository_baseline.py` | 通过 |
| API 测试 | `uv run --extra dev pytest -q -m "not integration"` | 通过：183 passed，1 skipped，1 deselected，222.97s |
| API 静态检查 | `uv run --extra dev ruff check app tests` | 通过 |
| PostgreSQL 迁移 | `alembic upgrade head` + `scripts/check_database_head.py` | 通过：空库升级到唯一 head `0011_control_plane_recovery` |
| PostgreSQL 并发回归 | 并发账本用例连续 3 次 | 通过：每轮 2 passed（3.25s、2.84s、2.81s） |
| Worker contract | POC 单测 + `workers/runpod-comfyui/contract/validate.py` | 通过：24 tests + validator |
| Web 单测 | `pnpm test` | 通过：1 test |
| Web Lint | `pnpm lint` | 通过 |
| Web Typecheck | `pnpm typecheck` | 通过：0 errors |
| Web Build | `pnpm build` | 通过 |
| OpenAPI Client | 导出 OpenAPI 后运行 `pnpm generate:client` | 通过；生成产物已更新 |
| Playwright | `pnpm test:e2e` | 通过：1 passed，22.2s |

## 5. 结论

SIM-05 的实现、离线模拟矩阵、恢复文档和全部门禁均已收口，状态为
`SIMULATION_ACCEPTED`。Docker 阻塞的根因是长期容器仍引用已删除的旧 Compose 网络；使用
本机现有镜像按依赖顺序重建容器后，所有长期服务均连接到当前 `video-maker_default` 网络，
冷启动日志不再出现旧网络缺失错误。验收过程中没有构建或拉取镜像，也没有删除数据卷。

WSL 仅在本次测试任务期间由隐藏进程保活，不配置登录自启。Playwright 使用当前源码、现有
镜像和本地 pnpm 存储启动临时 Web 容器，离线安装记录为 `downloaded 0`，测试结束后删除。

`SIMULATION_ACCEPTED` 不等于真实 POC、Benchmark 或生产就绪。真实 RunPod／
worker-comfyui 路线继续保持 `CONDITIONAL`／默认禁用，直到另一个获批且有凭据、有成本上限的
真实 POC 完成。

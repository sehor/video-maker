# WINDEV-02｜Local Workflow Runner

日期：2026-08-31。分支：`codex/windows-native-development`。
起始提交：`cd3c5ae`。

## 范围

实现开发专用 Local Runner，替换 WINDEV-01 的未就绪占位实现；复用现有
`GenerationExecutionService`、Outbox、Reconciler 和业务幂等保护。
不改变状态机、账本、结算、取消、供应商重试或媒体校验逻辑。
不实施 WINDEV-03～06，不连接 Hatchet Cloud，不迁移开发数据库。

## 行为

- `LocalWorkflowStarter.start()` 仅登记后台任务，立即返回稳定的 `local:` UUID。
  ID 由已有幂等键派生，长度不超过 Outbox 的 255 字符限制。
- 同一生命周期内，相同幂等键的运行中或成功任务复用同一执行；换绑其他 Job
  明确拒绝。失败或已取消的任务可用同一 ID 重入原有数据库幂等路径。
- 后台任务循环调用正式执行器，遵循其 `retry_after`；没有延迟的未完成步骤
  也会让出事件循环，避免争用数据库租约时忙等。
- FastAPI lifespan 先启动 Runner，再启动 Dispatcher/Reconciler；`/readyz`
  使用实际 Runner 状态。退出先取消后台调度，再取消并等待全部本地执行任务。
- Local 模式的 Reconciler 使用既有 `generation_workflow_key(job_id)` 再调度
  Runner，避免绕过去重创建另一个本地执行。Hatchet 的原执行路径保持不变。
- 构造和启动 Runner 均拒绝 production；跨事件循环调用明确失败。
- 测试不再维护 `InlineWorkflowStarter`。API fixture 使用 lifespan 中的正式
  Runner，通过 TestClient 的事件循环调度与等待；原有禁用执行的桩返回正式
  `ProviderExecutionStep`，不再用 `None` 冒充执行结果。

## 验证

Windows 原生 `uv`，未启动 WSL、Docker 或 Hatchet Server。复用已安装的
Windows PostgreSQL，单独创建本任务测试库 `video-maker_windev02_test`。
所有测试表的重建只发生在该独立 `_test` 库；`video-maker` 的表和迁移状态未改动。

测试进程的 `TEST_DATABASE_URL` 指向上述独立库，凭据仅在进程环境中传递。

| 检查 | 结果 |
|---|---|
| 调度与配置定向测试 | 32 项通过 |
| 真实业务闭环、Mock 与 Batch 定向回归 | 14 项通过 |
| PostgreSQL 完整 API 回归：`uv --cache-dir .uv-cache run --no-sync pytest -vv --tb=short --durations=5 -o faulthandler_timeout=30` | 218 项通过、2 项跳过，905.08 秒 |
| SQLite 补充回归：`test_domain_contracts.py`、`test_project_cleanup.py`、`test_provider_cancel_outbox.py` | 21 项通过，381.74 秒 |
| `uv --cache-dir .uv-cache run --no-sync ruff check app tests` | 通过 |
| `git diff --check` | 通过 |

跳过项为未显式启用的 Hatchet 集成测试，以及仅适用于 POSIX 的 ffprobe
进程组回归测试；不将其计入 Windows 已通过项。本次没有修改 Web 或 API
Schema，不执行 Web/E2E 门禁；完整原生开发栈验收仍由后续工作项负责。

原生 PostgreSQL 全量回归还暴露了三类旧测试 fixture 问题，均只修正测试：

- 取消与清理测试使用固定的 2026-08-31 零点，早于当天实际创建的 Outbox；
  改为创建事件后取得当前 UTC 时间，仍由可控时钟推进重试和租约。
- PostgreSQL 返回带时区的时间，SQLite 返回无时区时间；比较前只为后者
  补 UTC，不覆盖已有偏移。并为并发测试的事件等待增加超时，避免失败时挂起。
- PostgreSQL 建表会改变共享 Metadata 中延迟外键的编译状态；SQLite 契约
  fixture 使用独立 Metadata，保留原外键约束及违规写入必须失败的断言。

覆盖的验收条件：

| 条件 | 实际验证 |
|---|---|
| 快速接受、稳定 ID、幂等 | 阻塞执行器下立即返回，20 个并发重复启动只执行一次，成功后重放不再执行 |
| 多任务与退避 | 不同 Job 可并行等待，遵循执行器延迟，缺少延迟时也让出事件循环 |
| Mock 与模拟 Provider | API lifespan 的真实 Outbox Dispatcher 消费事件，产生可读取的 MP4 和唯一结算 |
| 就绪与生命周期 | Runner 在 Dispatcher 前启动；`/readyz` 的 workflow 检查为真；退出后全部已知任务结束 |
| 中断后恢复 | 已发布 Outbox 的模拟任务在 Runner 关闭后保持活动状态；新 Runner 通过 Reconciler 完成输出与结算 |
| 失败和等待取消 | 执行异常被记录，重入保持 ID；取消等待者不取消业务任务 |
| 生产保护 | production 配置与直接启动 Local Runner 均被拒绝 |
| Windows 路径 | 后台生成、对象读取使用带空格的本地存储目录 |

## 限制

Local Runner 只在一个进程、一个事件循环内去重，并保留本生命周期的已接受
任务记录；不是跨进程任务引擎。进程重启后业务恢复依赖 PostgreSQL 和既有
Reconciler，不提供 durable sleep 或即时恢复保证。中断恢复测试重建的是
Runner 生命周期，不声称模拟 Provider 的内存状态具备跨进程持久性。

开发库还需在 WINDEV-03 执行迁移；数据库或迁移检查失败时 `/readyz` 仍应返回
503，不能仅因 Runner 启动而报告整个服务就绪。生产继续使用 Hatchet。

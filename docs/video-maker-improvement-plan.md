# video-maker 改进开发计划（模拟执行链路版）

> 适用基线：`main` 分支，提交 `0d2434771a58c8756befd3ce4f976ba3b54a1fd1`  
> 目标：不依赖真实 RunPod 或真实 GPU，先完成可靠控制面、账本、媒体校验、任务恢复、取消、重试、素材 Claim、路由与产品边界，并同步完成高内聚、低耦合、可扩展、可维护的代码结构治理。  
> 原则：所有真实外部动作都可以由确定性模拟 Provider、模拟 Worker 和媒体 fixture 替代，但业务不变量、事务边界和恢复路径必须按生产标准实现。
>
> Issue 重写说明（2026-08-30）：第 6–9 节以 `89414439` 及当前工作树为执行基线，
> 替代旧基线生成的 B/Q Issue 列表、迁移顺序和完成定义。第 1–5 节保留为设计背景，
> 其中已经落地的能力不再重复立项；当前唯一执行清单以第 6 节为准。

---

## 1. 计划目标

完成后，系统应成为一个可用于内部验证和后续接入任意真实 Provider 的“生产级控制面”。它必须满足：

1. Job、Attempt、Quote、账本、Outbox、Provider Event、Output 之间不存在重复结算或部分提交。
2. API、Dispatcher、Hatchet Worker 或模拟 Provider 任意时刻中断后，任务都能继续、失败或正确返还。
3. Provider 返回成功不代表 Job 成功；只有实际媒体文件通过验证后才能发布最终 Output 并结算。
4. Webhook、Polling、重放、乱序事件和重复事件统一进入同一处理逻辑。
5. 取消动作可重试，数据库提交成功后不会因为进程崩溃而丢失 Provider Cancel。
6. 用户 API 不暴露 `mock_mode`、Provider Job ID、Workflow Version 等内部字段。
7. 模拟执行链路必须覆盖真实异步系统常见异常，而不是只提供“立即成功”假实现。
8. Router、Application、Domain、Infrastructure 的职责和依赖方向可由自动化门禁验证，核心代码不再依赖巨型服务和测试专用字段。

### 本计划明确不要求

- 不要求真实 RunPod 凭据。
- 不要求真实 GPU 推理。
- 不要求真实 ComfyUI Worker。
- 不要求真实支付平台。
- 不要求先完成多供应商。
- 不要求先做大规模性能优化。

---

## 2. 实施总原则

### 2.1 PostgreSQL 是业务唯一真相

Hatchet、模拟 Provider、Webhook 和日志只能驱动状态变化，不能成为 Job 最终状态和账本结果的唯一来源。

### 2.2 所有跨系统副作用必须可恢复

以下动作必须通过 Outbox 或持久工作流触发：

- 启动 Attempt；
- Provider Submit；
- Provider Cancel；
- Retry Attempt；
- 存储清理；
- 对账与补偿。

### 2.3 统一结果归并

Polling 与 Webhook 不得各自实现一套完成逻辑，统一调用：

```text
ProviderObservation
    ↓
ProviderObservationReducer
    ↓
WAIT / RETRY_CREATED / SUCCEEDED / FAILED_FINAL / CANCELLED / IGNORED
```

### 2.4 实际媒体事实高于 Provider 自报字段

最终 Output 的时长、尺寸、编码、帧率等字段只能来自 `MediaValidator`，不得直接采用 Provider 返回值。

### 2.5 模拟执行必须是确定且可恢复的

Control Plane 的 Job、Attempt、Outbox、事件和账本状态必须写入 PostgreSQL；时间推进使用
Fake Clock，外部结果由可编程模拟 Provider 和 Fault Injector 控制。模拟 Provider 作为进程内
测试适配器时可以保存内存状态，但测试必须通过重建执行服务验证 Control Plane 可从数据库恢复。
只有未来把模拟 Provider 部署成独立、可重启的开发服务时，才要求单独持久化其内部任务状态。

---

## 3. 目标架构

```text
FastAPI Router
    ↓
Application Command Service
    ├─ CreateGeneration
    ├─ CancelGeneration
    ├─ DeleteProject
    └─ ReconcileGeneration
    ↓
PostgreSQL Transaction
    ├─ Job / Attempt / Event
    ├─ Ledger
    └─ Outbox
    ↓
Outbox Dispatcher / Hatchet Workflow
    ↓
Provider Port
    ├─ SimulatedVideoProvider（本计划主实现）
    └─ RunPodVideoProvider（以后再接）
    ↓
ProviderObservationReducer
    ├─ WAIT
    ├─ RETRY_CREATED
    ├─ OUTPUT_READY
    ├─ FAILED_FINAL
    └─ CANCELLED
    ↓
OutputFinalizer
    ├─ Object stat / SHA-256
    ├─ FFprobe
    ├─ FFmpeg 完整解码
    ├─ GenerationOutput
    └─ Settle / Release
```

建议新增目录：

```text
apps/api/app/
├─ application/
│  └─ generation/
│     ├─ commands.py
│     ├─ submit.py
│     ├─ observe.py
│     ├─ finalize.py
│     ├─ cancel.py
│     └─ reconcile.py
├─ providers/
│  ├─ base.py
│  └─ simulated.py
├─ routers/
│  ├─ projects.py
│  ├─ generations.py
│  ├─ wallet.py
│  ├─ provider_webhooks.py
│  ├─ admin.py
│  └─ dev_simulation.py
└─ services/
   ├─ routing.py
   ├─ project_lifecycle.py
   └─ storage_cleanup.py
```

不要在第一步一次性移动全部文件。先完成功能，再逐阶段搬迁。

---

# 4. 代码质量专项主线

## 4.1 为什么必须单独设立代码质量主线

代码质量不能等业务问题全部修完后再“一次性重构”。当前项目已经出现几个明确的结构性信号：

- `api.py` 同时承担 HTTP 路由、权限查询、事务提交、对象构造、幂等处理和应用编排；
- `provider_execution.py` 同时承担 Submit、Poll、Webhook、Retry、Cancel、Output、状态推进和账本终结；
- `models.py` 集中放置用户、项目、素材、任务、账本、Outbox 和 Provider Inbox 等所有 ORM 模型；
- `mock_mode` 从测试场景泄漏到数据库、Public Schema、API 和前端页面；
- Router、Hatchet Task 和业务服务直接创建 `LocalObjectStorage`、`MockVideoProvider` 等具体实现；
- `get_settings()`、全局 `settings` 和全局 `workflow_starter` 使测试替换与多环境组合困难；
- 多处使用 `bool` 表示复杂处理结果，调用方容易忽略“创建了 Retry”等关键语义；
- 前端页面同时处理请求、业务状态、轮询、下载资源和 UI，生成客户端没有成为唯一 API 入口；
- README、Makefile、包管理器和锁文件尚未形成一致的仓库维护规范。

这些问题不会立即造成所有功能失败，但会显著提高后续修改媒体校验、持久 Poll、取消、路由和成本逻辑时的回归概率。因此，代码质量工作要与 M1–M9 并行推进，而不是只放在最后一个里程碑。

本专项的目标不是追求“层数越多越好”，也不是进行纯 DDD 重写，而是建立可验证的边界：

```text
高内聚：一个模块只因一类业务原因变化
低耦合：业务规则依赖端口，不依赖 FastAPI、Hatchet、Local Path 或具体 Provider
可扩展：增加 Provider、Storage、Route 时主要新增 Adapter，而不是修改整条主链路
可维护：事务边界、错误分类、状态推进和测试位置清晰，可安全局部修改
```

---

## 4.2 目标分层与依赖方向

采用渐进式四层结构：

```text
presentation
  HTTP Router / Request Schema / Response Schema
            ↓
application
  Use Case / Command / Query / Unit of Work / Port
            ↓
domain
  状态规则 / 失败分类 / 账本不变量 / 领域值对象
            ↑
infrastructure
  SQLAlchemy / Provider / Storage / Hatchet / FFmpeg Adapter

bootstrap
  只负责把 infrastructure 实现装配给 application
```

建议最终目录：

```text
apps/api/app/
├─ domain/
│  ├─ generation/
│  │  ├─ states.py
│  │  ├─ observations.py
│  │  ├─ failures.py
│  │  └─ rules.py
│  ├─ billing/
│  │  ├─ accounts.py
│  │  └─ rules.py
│  └─ routing/
│     └─ rules.py
├─ application/
│  ├─ ports/
│  │  ├─ clock.py
│  │  ├─ provider.py
│  │  ├─ storage.py
│  │  ├─ workflow.py
│  │  └─ unit_of_work.py
│  ├─ generation/
│  │  ├─ create.py
│  │  ├─ submit.py
│  │  ├─ observe.py
│  │  ├─ finalize.py
│  │  ├─ retry.py
│  │  ├─ cancel.py
│  │  └─ reconcile.py
│  ├─ projects/
│  │  └─ lifecycle.py
│  └─ billing/
│     ├─ quote.py
│     └─ settlement.py
├─ infrastructure/
│  ├─ db/
│  │  ├─ models/
│  │  ├─ repositories/
│  │  └─ sqlalchemy_uow.py
│  ├─ providers/
│  │  ├─ simulated.py
│  │  └─ registry.py
│  ├─ storage/
│  │  ├─ local.py
│  │  └─ remote.py
│  ├─ media/
│  │  └─ ffmpeg.py
│  └─ workflow/
│     └─ hatchet.py
├─ presentation/http/
│  ├─ routers/
│  ├─ schemas/
│  ├─ dependencies.py
│  └─ error_mapping.py
└─ bootstrap/
   ├─ container.py
   └─ app_factory.py
```

### 渐进迁移规则

第一轮不要求把 SQLAlchemy ORM 转换成完全独立的领域实体。采用两步走：

1. **先抽 Use Case、事务边界和 Port**，允许 Application Service 暂时操作 ORM 对象；
2. 行为稳定后再拆 ORM 文件、查询仓储和纯领域规则。

这样可以避免“为了架构而重写全部业务”，同时逐步纠正依赖方向。

### 强制依赖规则

1. `domain/` 不允许导入 FastAPI、SQLAlchemy、Hatchet、Pydantic Settings 或具体 Storage。
2. `application/` 不允许导入 FastAPI Router、具体 Provider、`LocalObjectStorage` 或 Hatchet SDK。
3. `infrastructure/` 可以实现 Application Port，但不能反向调用 Router。
4. `presentation/` 只能调用 Use Case 或 Query Service，不直接推进 Job/Attempt 状态。
5. 只有 `bootstrap/` 可以选择具体 Provider、Storage、Workflow 和 Clock 实现。
6. 测试专用场景不能进入核心 Job 实体；模拟配置必须放在 Dev/Simulation 边界。

新增 `scripts/check_architecture.py`，通过 AST 检查禁止导入关系；CI 中作为独立门禁运行，不依赖人工记忆。

---

## 4.3 可量化的质量门禁

以下数字是“需要解释的警戒线”，不是为了机械拆文件：

| 项目 | 门禁 |
|---|---|
| Router 函数 | 原则上不超过 40 行，不直接 `commit()`，不创建具体 Adapter |
| Application Use Case | 一个公开入口 `execute()`，一个明确事务边界 |
| 核心业务函数 | 原则上不超过 60 行，圈复杂度原则上不超过 10 |
| 核心业务模块 | 原则上不超过 500 行；超过时必须说明为何仍高内聚 |
| Import Cycle | 0 个 |
| 裸 `except Exception` | 只允许在进程边界、任务边界或 HTTP 边界；必须分类、记录并决定重试策略 |
| 业务处理返回值 | 禁止用无语义 `bool` 表示多种结果，改用 Enum + Dataclass Outcome |
| 配置访问 | `get_settings()` 只允许出现在 bootstrap、配置和少量基础设施工厂 |
| 具体实现创建 | 只允许在 Composition Root 或测试 Fixture |
| Python 类型检查 | Application、Domain、Port 必须通过静态类型检查 |
| 后端覆盖率 | 总体不低于 80%；账本、状态 Reducer、Finalizer、Retry、Cancel 分支覆盖率不低于 90% |
| 前端覆盖 | API 包装、轮询、幂等键、状态映射和关键表单必须有单测 |
| 架构文档 | 每次移动边界或修改契约必须同步 ADR 或模块说明 |

Ruff 规则分阶段启用，避免一次性产生大量无关改动：

```text
现有 E/F/I/UP/B
→ 增加 C90（复杂度）
→ 再选择性增加 SIM、RUF、PERF、TRY
```

新增规则必须先生成基线问题清单，再按模块消除，不能在单个业务 PR 中顺手改完整仓库。

---

## 4.4 Q0：现状依赖图与特征测试

### 目标

在移动代码之前确认“现在到底由谁调用谁”，并用测试锁定现有对外行为。

### 实施步骤

1. 编写 `scripts/report_code_quality.py`，输出：
   - Python 模块行数；
   - 函数长度；
   - 圈复杂度；
   - 直接调用 `db.commit()` 的位置；
   - 调用 `get_settings()` 的位置；
   - 创建 `LocalObjectStorage`、Provider、WorkflowStarter 的位置；
   - `except Exception` 的位置；
   - 模块导入关系和循环依赖。
2. 生成：

   ```text
   docs/architecture/current-module-map.md
   docs/architecture/current-dependency-graph.mmd
   docs/quality/baseline.json
   ```

3. 为将要拆分的入口添加 Characterization Tests：
   - 创建单 Job；
   - Batch 创建；
   - Cancel；
   - Webhook；
   - Retry；
   - Output Finalize；
   - Project 删除；
   - Public Job 响应。
4. 记录当前 OpenAPI Snapshot，重构 PR 默认不得改变 Public Contract。
5. 只做观测和测试，不移动生产代码。

### 验收

- 可以从报告中定位所有跨层直接依赖；
- 后续重构每个 PR 都能对比质量基线；
- Public Contract 与关键状态结果有测试保护。

---

## 4.5 Q1：Composition Root 与依赖注入

### 目标

消除 Router、Hatchet Task 和业务服务对具体实现的直接创建，使同一 Use Case 可以在 HTTP、Worker、测试和 Reconciler 中复用。

### 新增合同

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class UnitOfWork(Protocol):
    jobs: JobRepository
    attempts: AttemptRepository
    ledger: LedgerRepository
    outbox: OutboxRepository

    def __enter__(self) -> "UnitOfWork": ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...

@dataclass(frozen=True, slots=True)
class ApplicationServices:
    create_generation: CreateGeneration
    cancel_generation: CancelGeneration
    apply_observation: ApplyProviderObservation
    finalize_output: OutputFinalizer
    reconcile_generation: ReconcileGeneration
```

### 实施步骤

1. 新增 `bootstrap/container.py`：
   - 读取一次 Settings；
   - 创建 Session Factory；
   - 创建 Provider Registry；
   - 创建 Storage；
   - 创建 MediaValidator；
   - 创建 WorkflowStarter；
   - 创建 SystemClock；
   - 构造全部 Use Case。
2. 新增 `presentation/http/dependencies.py`，FastAPI 通过依赖函数获得 Use Case。
3. Hatchet Task 使用同一个 Container Factory，不再自行创建 LocalStorage。
4. 移除模块级 `workflow_starter` 和可变全局依赖。
5. `get_settings()` 从 Router 和 Application 层移除。
6. 测试使用 `TestContainer` 注入：
   - FrozenClock；
   - SimulatedProvider；
   - 临时 Storage；
   - Test UnitOfWork。
7. 不引入重量级 DI 框架；显式构造即可。

### 验收

- Application Use Case 可在不启动 FastAPI 的情况下单测；
- 更换 Provider 或 Storage 不需要修改 Router；
- Hatchet、Reconciler 与 HTTP 使用同一套应用服务；
- 具体 Adapter 只在 bootstrap 和测试 fixture 中实例化。

---

## 4.6 Q2：瘦 Router 与清晰的 Command/Query 边界

### 目标

Router 只负责协议适配，不再承载事务和状态机。

### Router 最终职责

```text
解析请求
→ 获取当前用户
→ 构造 Command
→ 调用 Use Case
→ 映射 Response
```

### 实施步骤

1. 先按资源拆 `api.py`：

   ```text
   routers/projects.py
   routers/assets.py
   routers/shots.py
   routers/generations.py
   routers/batches.py
   routers/wallet.py
   routers/provider_webhooks.py
   ```

   第一小步只移动代码，不改变行为。
2. 为写操作建立 Command：

   ```python
   CreateProjectCommand
   CreateGenerationCommand
   CreateBatchCommand
   CancelGenerationCommand
   DeleteProjectCommand
   ```

3. 为读操作建立 Query Service，不滥用通用 Repository：

   ```text
   ProjectQueries
   GenerationQueries
   LedgerQueries
   AdminGenerationQueries
   ```

4. 将 `owned_project()`、`owned_shot()` 等权限查询迁入 Query/Authorization Service。
5. 将 API 幂等获取、完成和回放封装为应用层组件：

   ```python
   with idempotency.execute(scope, key, payload) as execution:
       if execution.replayed:
           return execution.result
       result = use_case.execute(...)
       execution.complete(result)
   ```

6. Router 不再直接：
   - `db.add()`；
   - `db.commit()`；
   - 调用 `transition_job()`；
   - 调用 `finish_reservation()`；
   - 创建 Outbox；
   - 选择具体 Provider。
7. 每拆一个 Router，运行 OpenAPI Snapshot 和 E2E 回归。

### 验收

- Router 函数大多数在 15–40 行之间；
- Router 没有业务状态转换；
- Public Schema 与 ORM 模型不再默认一一绑定；
- HTTP 之外的任务入口可以复用同一个 Use Case。

---

## 4.7 Q3：Unit of Work 与事务边界统一

### 目标

让每个业务操作的原子范围显式可见，避免在不同函数深处多次 Commit。

### 原则

```text
一个 Use Case
→ 一个主事务
→ 事务内只做数据库状态和 Outbox
→ 外部副作用由事务后的 Worker/Dispatcher 完成
```

### 实施步骤

1. 实现 `SqlAlchemyUnitOfWork`，统一 Session 生命周期。
2. 只有 UnitOfWork 可以 `commit()` 和 `rollback()`；Repository 不 Commit。
3. 将以下操作各自收敛到单一事务：
   - Quote 使用 + Reserve + Job + Attempt + Submit Outbox；
   - Provider Observation + 状态推进 + Retry Attempt + Retry Outbox；
   - Output 发布 + Job/Attempt 成功 + Settle；
   - Cancel Requested + Cancel Outbox；
   - Project 软删除 + Cleanup Outbox。
4. 在 Application 层标注事务入口，内部 Helper 不自行开启顶层事务。
5. 保留数据库 CAS 和唯一约束作为最终并发保护，不依赖进程锁。
6. 使用故障注入 Hook 测试 Commit 前、Commit 后和响应丢失窗口。
7. 删除 Router、Provider Adapter 和 Storage Adapter 内的数据库提交。

### 验收

- 搜索生产代码中的 `.commit()`，仅出现在 UoW、迁移、少量基础设施进程边界；
- 任意跨系统动作都可由数据库状态或 Outbox 重建；
- 事务失败后不会出现 Job 已创建但未冻结、Retry 已创建但未调度等部分状态。

---

## 4.8 Q4：拆分 Generation 生命周期巨型服务

### 目标

彻底消除 `GenerationExecutionService` 的多重职责，但不进行一次性重写。

### 拆分顺序

#### 第一步：提取 ProviderObservationReducer

只负责：

```text
给定当前 Job/Attempt + Observation
→ 判定允许的状态推进
→ 返回 ApplyOutcome
```

它不调用 Provider、不读取文件、不发送 HTTP。

#### 第二步：提取 AttemptScheduler

负责：

```text
Submit Outbox
Poll 时间
Deadline
Retry Attempt
Retry Outbox
```

#### 第三步：提取 OutputFinalizer

负责：

```text
对象校验
媒体校验
最终 Output
成功状态
Settle
```

#### 第四步：提取 CancellationService

负责：

```text
Cancel Requested
Cancel Outbox
取消确认
Release
```

#### 第五步：提取 ReconciliationService

负责扫描没有下一动作的非终态记录，并恢复调度。

### 明确 Outcome

所有复杂处理返回：

```python
@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    action: ApplyAction
    job_id: UUID
    attempt_id: UUID
    next_attempt_id: UUID | None
    outbox_ids: tuple[UUID, ...]
```

禁止继续使用“`True` 表示可能需要继续循环，`False` 表示可能终态或等待”这类模糊合同。

### 删除 Facade

在全部调用方迁移后：

1. `GenerationExecutionService` 先变成薄 Facade；
2. 标记 Deprecated；
3. 删除 Facade；
4. 增加架构检查，禁止重新出现直接调用。

### 验收

- Submit、Observe、Finalize、Retry、Cancel、Reconcile 可分别单测；
- 修改取消策略不会影响媒体校验模块；
- 修改 Provider Adapter 不需要修改账本终结代码；
- 不再存在一个类同时依赖 Provider、Storage、Session、Ledger 和 Webhook Inbox。

---

## 4.9 Q5：ORM 模型、查询与数据库约束拆分

### 目标

降低 `models.py` 的认知负担，并确保数据库约束与应用查询口径一致。

### 建议目录

```text
infrastructure/db/models/
├─ base.py
├─ identity.py
├─ project.py
├─ generation.py
├─ billing.py
├─ outbox.py
├─ provider.py
└─ routing.py
```

### 实施步骤

1. 第一 PR 只拆文件，保持表名、字段、关系和迁移完全不变。
2. 提供 `models/__init__.py` 导出兼容名称，逐步迁移 Import。
3. 确保 Alembic `target_metadata` 导入全部模型，新增元数据完整性测试。
4. 将高频复杂查询移入专用 Repository/Query Object：
   - 当前有效 Attempt；
   - 可调度 Outbox；
   - 到期 Attempt；
   - User Job Detail；
   - Batch 状态聚合。
5. 修复约束与查询不一致：
   - Provider Job 唯一键的范围必须与 Webhook 查询范围一致；
   - 明确 `NULL` 时的唯一性策略；
   - final output、Attempt/Job 所属关系继续由复合外键保护。
6. 为 CheckConstraint、UniqueConstraint 和 ForeignKey 增加数据库级测试。
7. 对历史/审计实体取消危险的 ORM `delete-orphan` 或数据库级 Cascade，改为显式生命周期策略。
8. 不建立“万能 GenericRepository”；每个 Repository 只暴露 Use Case 真正需要的方法。

### 验收

- 模型拆分不生成数据库 Diff；
- 无循环 Import；
- 新增 Provider 时不需要修改现有模型模块之外的大量文件；
- 关键一致性即使绕过 API 也会被数据库拒绝。

---

## 4.10 Q6：错误模型、日志与可观测性统一

### 目标

让错误既能指导重试和业务终结，又不会把内部实现泄漏给用户。

### 错误分层

```text
DomainError
  非法状态、账本不变量、策略拒绝

ApplicationError
  NotFound、Conflict、Idempotency、Unavailable

InfrastructureError
  ProviderTimeout、StorageUnavailable、MediaToolFailure、DatabaseUnavailable

Presentation Error
  HTTP 状态码、Public Error Code、用户消息、request_id
```

### 实施步骤

1. 为 Provider、Storage、Media、Workflow 建立有类型的异常。
2. Application 层决定：
   - 重试；
   - 等待；
   - 最终失败；
   - 返回冲突；
   - 触发告警。
3. 只在以下边界捕获未知异常：
   - FastAPI Middleware；
   - Outbox Worker 主循环；
   - Hatchet Task 入口；
   - Reconciler 批处理入口。
4. 未知异常必须记录：

   ```text
   request_id
   job_id
   attempt_id
   outbox_id
   provider_code
   operation
   error_type
   retry_decision
   ```

5. Public API 只返回稳定错误码和用户消息；Admin API 可返回结构化技术原因，但不返回密钥、Claim 或完整 Provider Payload。
6. 日志字段命名形成文档，不在不同模块中混用 `job`, `job_id`, `generation_id`。
7. 增加日志脱敏测试，确保 Token、Claim Secret、Webhook Signature 不进入日志。

### 验收

- 不再通过字符串内容判断是否重试；
- Public Error Code 与内部异常类型解耦；
- 所有未知异常都有统一 request_id 和业务关联 ID；
- `except Exception` 数量显著下降且每处都有边界理由。

---

## 4.11 Q7：前端代码质量与 API 边界

### 目标

让页面组件以展示和交互为主，不再直接承载所有 API、轮询、幂等和资源清理逻辑。

### 目标结构

```text
apps/web/app/
├─ features/
│  ├─ projects/
│  │  ├─ api.ts
│  │  ├─ composables.ts
│  │  ├─ types.ts
│  │  └─ components/
│  ├─ generations/
│  │  ├─ api.ts
│  │  ├─ useGeneration.ts
│  │  ├─ useGenerationPolling.ts
│  │  └─ components/
│  ├─ assets/
│  └─ wallet/
├─ shared/
│  ├─ api/client.ts
│  ├─ api/errors.ts
│  ├─ idempotency.ts
│  └─ polling.ts
└─ pages/
```

### 实施步骤

1. 以生成客户端为类型和接口唯一来源，`useApi()` 只负责：
   - Token；
   - Base URL；
   - Request ID；
   - 统一错误解析；
   - Abort Signal；
   - 下载。
2. 每个业务 Feature 封装 API 方法，不在页面中手写路径和响应类型。
3. 稳定幂等键按“用户操作意图”生成：
   - 首次点击生成；
   - 网络重试复用同一个 Key；
   - 用户明确开始新任务时才生成新 Key。
4. 抽取 `useGenerationPolling()`：
   - 使用 AbortController；
   - 页面卸载停止；
   - 可见性变化时调整频率；
   - 终态停止；
   - 网络错误采用有限退避；
   - 不并发发起多个 Poll。
5. 下载 URL 生命周期封装，确保替换输出或卸载页面时 `URL.revokeObjectURL()`。
6. 页面不直接展示内部状态码；通过 `toPublicGenerationStage()` 映射用户状态。
7. 表单使用明确 Schema，避免散落的字符串和 `as` 类型断言。
8. 增加前端测试：
   - Token 获取失败；
   - API Error Code 映射；
   - 稳定幂等键重试；
   - Poll 终态停止；
   - 页面卸载取消请求；
   - Asset 选择与 Shot Reference；
   - Mock/Provider 字段不会出现在 Public 页面。
9. 开启严格 TypeScript 检查，并在 CI 中显式执行 `vue-tsc --noEmit`。

### 验收

- 页面组件不再包含重复的 fetch、轮询和错误解析；
- API 路径变化集中修改；
- 关键交互可在不启动后端时单测；
- Public 页面不依赖 Admin/Dev Schema。

---

## 4.12 Q8：静态门禁、测试分层和仓库治理

### 目标

把“保持代码质量”自动化，避免重构结束后再次退化。

### 后端门禁

```text
ruff check
ruff format --check
Python type check
architecture import check
pytest unit
pytest integration_postgres
pytest contract
coverage threshold
Alembic metadata/migration check
OpenAPI snapshot/client check
```

### 前端门禁

```text
ESLint
vue-tsc --noEmit
Vitest
Nuxt production build
Playwright critical loop
Generated client diff
```

### 测试分层

| 类型 | 负责 | 不负责 |
|---|---|---|
| Domain Unit | 状态规则、失败分类、账本计算 | DB、HTTP |
| Application Unit | Use Case、Outcome、事务调用顺序 | 真实 Provider |
| Repository Integration | PostgreSQL 锁、约束、查询 | 浏览器 |
| Adapter Contract | Provider、Storage、Workflow 合同 | 全产品流程 |
| HTTP Integration | 权限、Schema、错误映射、幂等 | GPU |
| E2E | 少量关键用户闭环 | 穷举故障场景 |

### 仓库治理

1. README 重写为真正的开发入口。
2. Makefile 全部统一为 pnpm，删除无用 `package-lock.json`。
3. 每个业务域增加 `README.md`，说明负责与不负责。
4. 使用 ADR 记录边界变化，不在 PR 描述中形成唯一知识来源。
5. 增加依赖升级策略：
   - 定期升级；
   - 锁文件唯一；
   - 升级 PR 不混业务功能；
   - 运行完整门禁。
6. 质量报告在 CI 中生成 Artifact，持续观察复杂度、覆盖率和模块大小趋势。

### 验收

- 新增跨层违规 Import 会直接导致 CI 失败；
- 覆盖率下降或生成客户端过期会直接失败；
- 仓库只保留一个 Node 包管理器和一个锁文件；
- 新开发者只读 README 即可启动、测试和理解模拟链路。

---

## 4.13 代码质量工作与业务里程碑的配合顺序

代码质量改造不能成为业务修复的前置大工程，也不能无限延期：

| 质量里程碑 | 最合适时机 | 不能晚于 |
|---|---|---|
| Q0 依赖图与特征测试 | 与 M0 同时 | M1 开始前 |
| Q1 Composition Root | M1 模拟 Provider 开始时 | M2 Finalizer 接入前 |
| Q2 瘦 Router | M1–M3 逐资源迁移 | M7 Public API 分离前 |
| Q3 Unit of Work | M2–M4 并行 | M5 Cancel Outbox 前 |
| Q4 Generation 服务拆分 | 随 M2–M5 按职责提取 | M5 完成时 |
| Q5 ORM/查询拆分 | M5–M6 行为稳定后 | M9 Route 模型扩展前 |
| Q6 错误与日志 | 各新模块创建时同步完成 | M10 前 |
| Q7 前端质量 | M7 Public API 分离时 | M8 素材 UI 完成前 |
| Q8 自动门禁 | 从 M0 逐步启用 | 阶段总验收前 |

### PR 粒度规则

一个 PR 应优先属于以下一种类型：

```text
行为修复
结构重构
数据库迁移
测试/门禁
文档
```

必要时可以在行为 PR 中做局部提取，但不得同时：

- 重写整个目录；
- 修改大量 Public Contract；
- 升级全部依赖；
- 格式化全仓库；
- 改变多个业务不变量。

每个重构 PR 必须说明：

```text
行为是否改变
依赖方向如何改善
删除了哪些旧入口
新增了哪些测试
是否产生临时兼容层及删除期限
```

---

## 4.14 现有文件到目标模块的迁移映射

| 现有文件 | 目标拆分 | 主要原因 |
|---|---|---|
| `app/api.py` | `presentation/http/routers/*` + Application Use Cases | 协议、事务、业务混合 |
| `app/provider_execution.py` | `submit.py`、`observe.py`、`retry.py`、`finalize.py`、`cancel.py`、`reconcile.py` | 生命周期职责过多 |
| `app/models.py` | `infrastructure/db/models/*` | 所有业务域集中 |
| `app/ledger.py` | `application/billing/*` + `domain/billing/*` + Repository | 规则、SQL 和应用流程混合 |
| `app/storage.py` | Application Port + Local/Remote Adapter + Claim Codec | 合同与具体实现混合 |
| `app/provider.py` | Application Provider Port + DTO + Simulated Adapter | Port 和 Mock 实现同文件 |
| `app/hatchet_workflows.py` | `infrastructure/workflow/hatchet.py` | Task 内直接组装具体服务 |
| `app/main.py` | `bootstrap/app_factory.py` + middleware | 模块导入时创建全局应用依赖 |
| `apps/web/app/composables/useApi.ts` | `shared/api/client.ts` + Feature API | 通用请求与业务调用混合 |
| `apps/web/app/pages/*` | 薄 Page + Feature Composable/Component | 页面承担业务状态和请求细节 |

迁移时旧文件可保留临时导出，但每个兼容层都要标记删除 Issue，不能永久存在两套入口。

---

## 4.15 代码质量完成定义

代码质量专项只有同时满足以下条件才能关闭：

- [ ] Router 不直接提交事务、推进状态或创建具体 Adapter；
- [ ] Application Use Case 的事务边界清晰，每个写操作只有一个主事务；
- [ ] Domain/Application 不依赖 FastAPI、Hatchet SDK 或 Local Path；
- [ ] 具体 Provider、Storage、Workflow 只在 Composition Root 创建；
- [ ] `GenerationExecutionService` 巨型职责已被拆除并删除；
- [ ] `models.py` 已按业务域拆分且无数据库 Schema 意外变化；
- [ ] Poll、Webhook、Retry、Cancel 和 Finalize 使用有语义的 Outcome，不再依赖模糊 `bool`；
- [ ] 核心模块无循环 Import，架构检查进入 CI；
- [ ] `get_settings()` 不再散落于 Router 和 Application 层；
- [ ] 未知异常只在明确边界捕获，并有分类、关联 ID 和重试决策；
- [ ] 后端总覆盖率达到约定门槛，可靠性核心模块分支覆盖率达到 90%；
- [ ] 前端页面变薄，轮询、幂等、错误解析和下载资源有独立测试；
- [ ] Public Schema 与 Admin/Dev Schema 完全分离；
- [ ] `mock_mode` 等测试概念不再存在于核心 Job 模型；
- [ ] README、ADR、模块说明和 Runbook 与当前实现一致；
- [ ] pnpm、锁文件、Makefile 和 CI 命令保持统一。

### 明确不做的“伪代码质量改进”

以下动作不能单独视为质量提升：

- 只把一个大文件机械切成多个同样互相调用的大文件；
- 为每张表创建通用 Repository，但业务规则仍散落在 Router；
- 引入复杂 DI 框架，却没有纠正依赖方向；
- 为了行数指标拆出大量只有一行的 Helper；
- 在没有特征测试时进行大规模改名或搬迁；
- 把单体拆成微服务；
- 同一个 PR 同时做功能、架构、依赖升级和全仓格式化。

真正的验收标准是：修改一个业务规则时，需要触碰的模块更少，测试定位更直接，副作用边界更清楚，新增 Adapter 时旧代码修改更少。

---

# 5. 业务可靠性里程碑与详细实施步骤

## M0：建立可靠基线和测试门禁

### 目标

在修改核心链路前，固定当前行为，区分 SQLite 快速测试与 PostgreSQL 并发测试。

### 实施步骤

1. 新增 `postgres-test` 或独立测试数据库，名称必须以 `_test` 结尾。
2. CI 显式设置：

   ```text
   TEST_DATABASE_URL=postgresql+psycopg://.../video_factory_test
   ```

3. 将测试分为：

   ```text
   unit          不依赖 Docker，可用 SQLite
   integration   使用 PostgreSQL
   orchestration 使用 PostgreSQL + Hatchet 或模拟 WorkflowStarter
   e2e           Web + API + PostgreSQL
   ```

4. 增加测试 marker：

   ```python
   integration_postgres
   orchestration
   media
   ```

5. CI 增加：

   - `ruff format --check`；
   - PostgreSQL 集成测试；
   - Alembic 从空库升级；
   - Alembic 从当前阶段库升级；
   - `vue-tsc --noEmit`；
   - OpenAPI Client 差异检查继续保留。

6. 为以下现有行为补回归测试：

   - Reserve、Settle、Release 各最多一次；
   - Quote 同一时间只使用一次；
   - Outbox 双 Dispatcher 只能领取一次；
   - Poll 与 Webhook 同时完成只创建一个 Output；
   - 旧 Attempt 事件不能覆盖新 Attempt 成功状态。

### 验收标准

- 并发和锁相关测试实际运行在 PostgreSQL 上。
- SQLite 与 PostgreSQL 测试结果不存在行为差异。
- 当前 `main` 全量测试通过后才能开始 M1。

---

## M1：实现持久化模拟 Provider

### 目标

用模拟数据替代真实执行，但保留异步、断连、重试、乱序、取消和媒体异常等真实特征。

### 数据库变更

新增迁移 `0008_simulated_provider.py`，增加：

```text
simulated_provider_jobs
- id UUID
- provider_job_id VARCHAR UNIQUE
- attempt_id UUID UNIQUE
- scenario_code VARCHAR
- state VARCHAR
- poll_count INT
- submit_count INT
- cancel_count INT
- webhook_count INT
- next_transition_at TIMESTAMPTZ NULL
- output_object_key VARCHAR NULL
- result_payload_json JSON
- created_at / updated_at
```

可选增加：

```text
simulated_provider_events
- external_event_id
- provider_job_id
- sequence_no
- payload_json
- delivered_at
```

### 新增代码

```text
app/providers/simulated.py
app/providers/scenarios.py
app/providers/registry.py
```

### 场景定义

至少支持：

```text
SUCCESS_AFTER_2_POLLS
SUCCESS_WITHOUT_WEBHOOK
DUPLICATE_WEBHOOK
OUT_OF_ORDER_WEBHOOK
SUBMIT_UNKNOWN_THEN_FOUND
POLL_EXCEPTION_THEN_SUCCESS
RETRYABLE_FAILURE_THEN_SUCCESS
FINAL_FAILURE
CANCEL_ACCEPTED_THEN_CONFIRMED
CANCEL_REQUEST_TIMEOUT_THEN_CONFIRMED
CORRUPT_MP4_WITH_FTYP
WRONG_CODEC
WRONG_RESOLUTION
WRONG_DURATION
CHECKSUM_MISMATCH
```

### Provider 行为

1. `submit()`：
   - 使用 Attempt Idempotency Key 创建唯一模拟任务；
   - 重复 submit 返回同一 Provider Job ID；
   - `SUBMIT_UNKNOWN_THEN_FOUND` 第一次返回 UNKNOWN，但数据库中保留任务。

2. `poll()`：
   - 每次 Poll 原子增加 `poll_count`；
   - 根据场景和次数推进状态；
   - 不使用不可控的长 `sleep`；
   - 使用注入 Clock 或数据库时间。

3. `cancel()`：
   - 记录 `cancel_count`；
   - 可先返回 accepted 但状态仍 RUNNING；
   - 后续 Poll 才返回 CANCELLED。

4. 输出：
   - 使用仓库 fixture；
   - 通过系统签发的 Write Claim 写入 Storage；
   - 返回对象引用、大小和 SHA-256；
   - 不返回可信的最终媒体元数据。

### Dev 控制接口

仅非生产环境启用：

```text
POST /v1/dev/simulations
POST /v1/dev/simulations/{provider_job_id}/emit-webhook
POST /v1/dev/simulations/{provider_job_id}/advance
GET  /v1/dev/simulations/{provider_job_id}
```

这些接口必须被 `environment != production` 保护，且不能进入 Public OpenAPI Client。

### 验收标准

- API、Worker、Dispatcher 任意重启后，模拟任务状态仍保留。
- 同一 Attempt 重复 Submit 不产生第二个 Provider Job。
- 所有场景可由自动测试稳定复现。
- 模拟场景配置不再存入 `GenerationJob.mock_mode`。

---

## M2：接通媒体校验与 Output Finalizer

### 目标

修复“只看 `ftyp` 就成功结算”的问题。

### 新增模块

```text
app/application/generation/finalize.py
```

定义：

```python
@dataclass(frozen=True)
class OutputCandidate:
    attempt_id: UUID
    object_key: str
    media_type: str
    expected_size_bytes: int
    expected_sha256: str

class OutputFinalizer:
    def finalize(candidate: OutputCandidate) -> FinalizeOutcome:
        ...
```

### 实施步骤

1. Provider 输出改为对象引用，不再把 `content` 和最终媒体元数据直接传给领域层。
2. Provider 输出先进入 `candidate-outputs/` 或 `quarantine/` 命名空间。
3. Finalizer 执行：

   ```text
   Storage.stat
   → 比较 size
   → 比较 SHA-256
   → 读取到受控临时文件
   → MediaValidator.validate
   → 获得 MediaFacts
   → 写 GenerationOutput
   → 设置 final_output_id
   → Attempt SUCCEEDED
   → Job SUCCEEDED
   → settle
   ```

4. `GenerationOutput.duration_ms/width/height/fps/codec` 只能从 `MediaFacts` 填充。
5. 增加 `pixel_format`、`container`、`decodable` 字段，必要时新增迁移。
6. 校验失败时：
   - Attempt 标记失败；
   - Job 根据重试策略创建新 Attempt 或最终失败；
   - 不设置 `final_output_id`；
   - 不结算；
   - 执行 Release 或等待 Retry。

7. 不在数据库事务失败时立即删除候选对象。使用确定性 key 或清理 Outbox，保证重放安全。
8. `finalize()` 必须幂等：同一个 Attempt 重放只得到同一 Output 或同一终态。

### 必须增加的测试

- 正确 H.264 720p 视频成功；
- 包含 `ftyp` 但不可解码的视频失败；
- 错误 codec 失败；
- 错误分辨率失败；
- 错误时长失败；
- SHA-256 不一致失败；
- 两个并发 Finalizer 只创建一个最终 Output；
- Finalizer 在 DB Commit 前崩溃，重放后仍只结算一次；
- Finalizer 在 DB Commit 后响应丢失，重放后仍只结算一次。

### 验收标准

- `_finish_output()` 中不再存在简单 `ftyp` 判断。
- Provider 自报 width、duration、codec 不影响最终 Output。
- 媒体校验失败永远不会发生 Settle。

---

## M3：实现持久任务循环和 Reconciler

### 目标

解决任务只 Poll 一次、无 Webhook 时永久卡住的问题。

### 数据库变更

新增迁移 `0009_attempt_scheduling.py`：

```text
generation_attempts
- submitted_at TIMESTAMPTZ NULL
- last_polled_at TIMESTAMPTZ NULL
- next_poll_at TIMESTAMPTZ NULL
- deadline_at TIMESTAMPTZ NULL
- poll_count INT NOT NULL DEFAULT 0
- reconcile_count INT NOT NULL DEFAULT 0
- last_provider_error VARCHAR NULL
```

新增索引：

```text
(status, next_poll_at)
(status, deadline_at)
```

### Hatchet 工作流改造

将当前一次性任务改成：

```text
Orchestrate
  ├─ SubmitAttempt
  ├─ Durable Sleep
  ├─ PollAttempt
  ├─ ApplyObservation
  └─ 未终态则循环
```

每个子任务必须可重放，且数据库副作用幂等。

### 非 Hatchet 补偿路径

新增：

```text
app/application/generation/reconcile.py
```

提供：

```python
reconcile_due_attempts(limit=100)
reconcile_stuck_jobs(limit=100)
reconcile_provider_inbox(limit=100)
```

由独立进程或定时 Hatchet Workflow 调用。

### 具体规则

1. CREATED：确保有 Submit Outbox。
2. SUBMITTING：
   - 未知提交结果时查询 Provider；
   - 不盲目重复 Submit。
3. SUBMITTED/RUNNING：
   - 到 `next_poll_at` 执行 Poll；
   - 指数退避，例如 1s、2s、4s、8s，设置最大间隔。
4. 超过 `deadline_at`：
   - 进入 TIMED_OUT；
   - 根据策略 Retry 或最终 Release。
5. Webhook 丢失：Polling 最终完成。
6. Polling 失败：记录错误并重新调度，而不是结束所有处理。
7. 所有时间计算使用注入 Clock，测试不依赖真实等待。

### 验收测试

- SUCCESS_AFTER_2_POLLS 最终成功；
- SUCCESS_WITHOUT_WEBHOOK 最终成功；
- Worker 在第一次 Poll 后崩溃，重启后继续；
- Dispatcher/Worker 双实例不重复执行最终副作用；
- Poll 连续异常后恢复成功；
- 超时后最多 Retry 指定次数；
- 到达最大 Attempt 后只 Release 一次。

### 验收标准

- 不存在“PENDING/RUNNING 后 execute 直接永久结束”的路径。
- 数据库中所有非终态 Attempt 都有明确的下一动作或截止时间。

---

## M4：统一 Webhook、Polling、重试与事件归并

### 目标

解决 Webhook 可重试失败后新 Attempt 不执行，以及 Poll/Webhook 分支行为不一致的问题。

### 新增类型

```python
class ApplyAction(StrEnum):
    WAIT = "WAIT"
    RETRY_CREATED = "RETRY_CREATED"
    OUTPUT_READY = "OUTPUT_READY"
    TERMINAL_FAILED = "TERMINAL_FAILED"
    TERMINAL_CANCELLED = "TERMINAL_CANCELLED"
    IGNORED = "IGNORED"

@dataclass(frozen=True)
class ApplyOutcome:
    action: ApplyAction
    job_id: UUID
    attempt_id: UUID
    next_attempt_id: UUID | None = None
```

### 实施步骤

1. 将 `_apply_provider_result()` 改成纯粹的统一归并入口。
2. Poll 和 Webhook 都构造 `ProviderObservation`。
3. Reducer 在单个数据库事务中：
   - 检查 Job 是否终态；
   - 检查 Attempt 是否当前有效 Attempt；
   - 检查状态是否允许推进；
   - 写 JobEvent；
   - 必要时创建 Retry Attempt；
   - 同事务写 Retry Outbox。
4. Webhook 路径必须处理 `RETRY_CREATED`，而不是忽略返回值。
5. 旧 Attempt 的迟到事件标记为 `IGNORED`，保留审计，但不改写 Job。
6. 同一 `external_event_id` 不同 payload hash：
   - 记录冲突；
   - 不处理第二个 payload；
   - 增加告警指标。
7. Provider Inbox 处理状态完整使用：

   ```text
   RECEIVED → PROCESSING → PROCESSED
                          → IGNORED
                          → RECEIVED（可重试）
   ```

8. Event Inbox 的“业务处理”和“标记 PROCESSED”尽量在一个事务边界内完成，或通过可安全重放的两阶段逻辑保证一致性。

### 测试矩阵

- Webhook retryable failure → Attempt 2 自动执行并成功；
- Poll retryable failure → Attempt 2 自动执行并成功；
- Webhook 与 Poll 同时成功 → 只有一个 Output；
- 旧 Attempt 成功回调 → IGNORED；
- 重复 Webhook → 只处理一次；
- 相同 Event ID 不同 payload → 冲突记录，不改状态；
- Webhook 处理 Commit 后响应丢失 → 重放返回同一结果。

### 验收标准

- Webhook 和 Poll 不再调用不同的终结实现。
- 创建 Retry Attempt 与安排 Retry 执行必须同事务完成。

---

## M5：实现可靠取消 Outbox

### 目标

解决 `CANCEL_REQUESTED` 提交后进程崩溃导致 Provider Cancel 丢失的问题。

### 数据库与事件

新增 Outbox 类型：

```text
generation.attempt.cancel
```

稳定键：

```text
attempt:{attempt_id}:cancel:v1
```

可选增加：

```text
generation_jobs.cancel_requested_at
generation_jobs.cancel_deadline_at
```

### API 改造

取消接口只做事务内操作：

```text
验证权限
→ API 幂等
→ Job → CANCEL_REQUESTED
→ 写 cancel outbox
→ commit
→ 返回 202/当前 Job
```

API 线程不直接调用 Provider。

### Cancel Worker

1. 领取 Cancel Outbox。
2. 调用 Provider.cancel。
3. 记录 Provider Observation。
4. Provider 返回 CANCELLED 时：
   - Attempt CANCELLED；
   - Job CANCELLED；
   - Release；
   - 清理待发布候选输出。
5. Provider 返回 accepted/pending 时：
   - 保持 CANCEL_REQUESTED；
   - 安排后续 Poll。
6. Provider 调用异常时：
   - Outbox 重试；
   - 指数退避；
   - 不提前 Release。
7. 如果取消期间出现有效成功输出：
   - 按明确策略处理；
   - 默认“Provider 已产生有效结果则成功并结算”，除非产品另有定义；
   - 该规则必须写进文档和测试。

### 测试

- Cancel DB commit 后进程崩溃，重启后仍发送 Cancel；
- 重复取消只产生一个 Outbox；
- Provider Cancel 调用三次失败后成功；
- Cancel accepted 但未确认，不 Release；
- Cancel 确认后只 Release 一次；
- Cancel 与成功回调竞争，终态和账本结果唯一。

### 验收标准

- API 层不再直接调用 Provider.cancel。
- 任意取消请求最终有终态、下一动作或告警，不会静默永久卡住。

---

## M6：项目删除、素材和对象存储生命周期

### 目标

避免硬删除造成审计丢失、外键异常和 Storage 孤儿。

### 数据库变更

新增：

```text
projects.status = ACTIVE / DELETION_REQUESTED / DELETED
projects.deleted_at
project_assets.deleted_at
```

建议保留 Job、Attempt、Output、Ledger、JobEvent，不做物理级联删除。

新增 Outbox 类型：

```text
storage.object.delete
project.cleanup
```

### 实施步骤

1. 将 `DELETE /projects/{id}` 改为软删除命令。
2. 如果存在活动 Job：
   - 第一版直接返回 `409 PROJECT_HAS_ACTIVE_JOBS`；
   - 不自动批量取消，避免范围过大。
3. 无活动 Job 时：
   - Project → DELETION_REQUESTED；
   - Asset → DELETED；
   - 写清理 Outbox；
   - commit。
4. 清理 Worker 删除未保留的 Asset 对象。
5. Output 是否删除按保留策略决定；开发阶段可保留，后续增加 retention。
6. API 查询默认过滤 DELETED 项目。
7. 重复删除返回当前删除状态。
8. Storage delete 失败进入重试和 Dead Letter，不回滚业务软删除。

### 测试

- 有运行任务时拒绝删除；
- 有历史任务时软删除但保留 Job/Event/Ledger；
- Storage 删除失败后可重试；
- 重复删除幂等；
- 其他用户不能删除；
- 已删除项目不能新增 Shot 或 Asset。

### 验收标准

- 不再执行 `db.delete(project)` 物理删除主业务对象。
- 项目删除不产生未分类数据库 500。

---

## M7：Public API、Admin API 与 Dev Simulation 分离

### 目标

把开发测试能力从用户产品接口中移除。

### Schema 拆分

新增：

```text
PublicGenerationCreate
PublicGenerationJobOut
PublicGenerationProgressOut
AdminGenerationJobOut
AdminGenerationAttemptOut
DevSimulationCreate
```

### Public API 不返回

- `provider_code`
- `provider_job_id`
- `workflow_version`
- `mock_mode`
- 原始内部事件 payload
- 内部错误栈

Public Job 可以返回：

```text
id
status
progress_stage
user_message
failure_category
final_output_id
created_at
updated_at
```

### Admin API

```text
GET /v1/admin/jobs/{id}
GET /v1/admin/jobs/{id}/attempts
GET /v1/admin/jobs/{id}/events
POST /v1/admin/jobs/{id}/reconcile
```

先实现最小只读和受控 reconcile，不必立即实现完整后台 UI。

### 前端改造

1. 镜头生成页面删除 Mock 场景选择。
2. 测试额度按钮移到开发工具页，生产构建不显示。
3. Job 页面显示用户友好的阶段：

   ```text
   排队中
   正在生成
   正在校验
   已完成
   已取消
   生成失败
   ```

4. 内部事件时间线只放到 Admin/Dev 页面。
5. 前端一次用户操作生成一个稳定 Idempotency-Key，并在网络重试时复用。

### 数据迁移

1. 新增独立模拟场景关联字段或配置表。
2. 测试全部迁移后移除 `GenerationJob.mock_mode`。
3. 从 Public OpenAPI 删除模拟字段。

### 验收标准

- 正常用户无法选择“损坏 MP4”“Provider 失败”等模拟情形。
- 生产环境没有 Dev Simulation 路由。
- 模拟 Provider 仍可通过测试和 Dev 工具完整驱动。

---

## M8：把 Asset / ShotReference 接入生成合同

### 目标

让当前已有素材模型真正进入生成链路，同时仍然使用模拟 Worker。

### Worker Contract

定义固定输入：

```python
class WorkerGenerationInput(BaseModel):
    job_id: UUID
    attempt_id: UUID
    workflow_version: Literal["simulated_i2v_720_v1"]
    prompt: str
    duration_ms: int
    aspect_ratio: Literal["16:9", "9:16"]
    input_claims: list[InputClaim]
    output_claim: OutputClaim
    callback_claim: CallbackClaim
```

禁止：

- 任意 URL；
- 任意本地路径；
- 任意 workflow JSON；
- 任意模型名；
- 任意节点参数。

### 后端实施

1. Generation 创建时加载 ShotReference。
2. 校验 Asset：
   - 同一 Project；
   - READY；
   - MIME 在白名单；
   - 尚未删除。
3. 为输入对象签发短期 Read Claim。
4. 为候选输出签发 Write Claim。
5. Simulated Provider 必须实际使用 Claim 读取输入并写输出。
6. Claim 过期时返回明确错误，并由控制面按策略处理。

### 前端实施

1. 项目页展示素材列表，不只保存最近上传的一个 Asset。
2. 创建或编辑 Shot 时可选择 `SOURCE_IMAGE`。
3. Shot 页面展示当前引用。
4. Generation 前若路线要求参考图而 Shot 未配置，前端和 API 都拒绝。

### 测试

- 跨项目 Asset 不能引用；
- 过期 Claim 被拒绝；
- Read Claim 不能用于 Write；
- Provider 不能接受任意 URL；
- Shot 无参考图时 I2V Route 不可生成；
- 删除 Asset 后已有历史 Job 仍可审计，但新 Job 不可使用。

### 验收标准

- 生成链路中不直接传 Local Path。
- Simulated Provider 的输入输出与以后真实 Worker 使用同一合同。

---

## M9：实现不可变 Route、质量档与模拟成本

### 目标

让 FAST/STUDIO 不再只是账本名称，而是绑定真实存在的能力版本；真实执行仍由模拟 Provider 完成。

### 数据库变更

新增：

```text
route_versions
- id
- route_code
- version
- resolution
- workflow_version
- provider_code
- enabled
- immutable_config_json
- created_at

route_candidates
- id
- route_version_id
- provider_endpoint_key
- priority
- enabled
- cost_policy_json
```

为 Project 或 Job 保存不可变 route version snapshot。

### 实施步骤

1. 初始只建立：

   ```text
   simulated_i2v_720_v1
   ```

2. Quote 时校验：
   - Tier 是否启用；
   - 对应 Route 是否启用；
   - Resolution 是否真实可用；
   - Shot 是否满足素材要求。
3. 创建 Job 时选择并冻结 Route Version。
4. Retry 默认使用同一 Route Version，可切换 Candidate，但历史版本不可变。
5. 增加 Route Kill Switch：关闭后不接受新 Quote/Job，历史任务继续处理。
6. Simulated Provider 返回确定性成本：

   ```text
   amount_minor
   currency
   source=ESTIMATED
   queue_ms
   run_ms
   billed_ms
   ```

7. 将成本保存到 Attempt。
8. 增加最小对账命令：

   ```text
   Job settlement ↔ Attempt cost ↔ Output duration
   ```

### 验收标准

- 未绑定可用 Route 的 Tier 不可报价。
- 1080p 在没有 Route 时不可售卖。
- Route 被关闭后新 Job 拒绝，已存在 Job 不被篡改。
- 每个成功 Output 可追溯到 Attempt、Route Version、Workflow Version 和模拟成本。

---

## M10：模块拆分、维护性与运行保障

### 目标

在行为稳定后进行结构重构，降低 `api.py`、`provider_execution.py` 和 `models.py` 的职责密度。

### 后端拆分顺序

1. 先拆 Router，不改业务：

   ```text
   routers/projects.py
   routers/assets.py
   routers/generations.py
   routers/wallet.py
   routers/webhooks.py
   ```

2. 将 Provider Event Inbox 逻辑迁移到 `observe.py`。
3. 将 Output 完成逻辑迁移到 `finalize.py`。
4. 将 Cancel 迁移到 `cancel.py`。
5. 将调度和补偿迁移到 `reconcile.py`。
6. `provider_execution.py` 最终只保留兼容 facade，随后删除。
7. ORM 模型按业务域拆分，但避免一次性大迁移：

   ```text
   models/core.py
   models/generation.py
   models/ledger.py
   models/storage.py
   models/routing.py
   ```

### 依赖注入

增加组合根：

```text
app/container.py
```

负责创建：

- SessionFactory
- ProviderRegistry
- Storage
- MediaValidator
- WorkflowStarter
- Clock

业务服务不直接调用 `get_settings()` 或创建 LocalStorage。

### Outbox 运营能力

增加：

- 指数退避；
- 最大重试次数；
- Dead Letter 状态；
- 最后错误类型；
- 指标：待处理数、最老事件年龄、重试数、Dead Letter 数。

### Health/Readiness

拆分：

```text
/healthz    进程存活
/readyz     DB、Storage、必要配置可用
```

Hatchet 和模拟 Provider 可作为独立详细检查，不要求每次 ready 都做昂贵调用。

### 文档

1. 重写 README：
   - 架构；
   - 启动方式；
   - 测试方式；
   - 模拟场景；
   - 数据库迁移；
   - 常见故障恢复。
2. 新增 ADR：
   - 持久化模拟 Provider；
   - Provider Observation Reducer；
   - Output Finalizer；
   - Cancel Outbox；
   - 项目软删除。
3. 新增 Runbook：
   - Job 卡在 SUBMITTING；
   - Job 卡在 CANCEL_REQUESTED；
   - Outbox Dead Letter；
   - Storage 对象缺失；
   - 账本对账异常。
4. 统一包管理：删除无用 `package-lock.json`，Makefile 全部使用 pnpm。

### 验收标准

- 核心业务服务可使用 Fake Clock、Fake Provider、Fake Storage 单测。
- Router 中不再包含复杂事务和状态推进逻辑。
- `provider_execution.py` 不再承担全部生命周期职责。

---

# 6. 当前剩余 Issues / PR（纯模拟链路）

本节以当前工作树为基线，替代原 B1–B13、Q0–Q8 拆分。已经通过现有测试覆盖的
媒体校验、持久 Poll、统一完成路径、账本互斥、Public API 隔离、Asset Claim、
Route 与成本溯源不再重复立项。

所有 Issue 均使用 Fake Clock、模拟 Provider、模拟 Storage、媒体 fixture 和故障注入
完成验收；不申请 RunPod 凭据，不启动真实 GPU，不提交真实 Provider 任务，不产生付费
Benchmark。最终结论只能表述为“模拟控制面验收通过”，不得表述为真实 RunPod、真实
视频路线或生产就绪。

## 6.1 执行顺序

| 顺序 | ID | Issue | 依赖 | 优先级 |
|---:|---|---|---|---|
| 1 | SIM-01 | 修复 CI、迁移和仓库命令基线 | 无 | P0 |
| 2 | SIM-02 | Provider Cancel Outbox 与崩溃恢复 | SIM-01 | P0 |
| 3 | SIM-03 | Project 软删除与 Storage Cleanup Outbox | SIM-01 | P0 |
| 4 | SIM-04 | Reconciler、Dead Letter 与 Readiness | SIM-02、SIM-03 | P1 |
| 5 | SIM-05 | 模拟故障注入总验收与文档收口 | SIM-01–SIM-04 | P1 |

每个 Issue 对应一个独立 PR。只允许为当前 Issue 做必要的局部提取；不把完整四层架构、
全量 Repository/UoW、`models.py` 全拆分或删除所有 Facade 作为前置工程。

## 6.2 SIM-01：修复 CI、迁移和仓库命令基线

### 目标

让当前仓库的测试与静态检查入口真实可执行，避免迁移头和命令文档继续引用旧基线。

### 范围

- CI 从空 PostgreSQL 数据库升级到当前唯一 Alembic head；不再硬编码已经过期的 `0007`。
- 校验历史迁移到当前 head 的连续升级路径。
- Makefile、CI、README 和开发文档统一使用 `pnpm`，仓库只保留一个锁文件。
- 保留现有 Pytest、Ruff、Vitest、ESLint、Build 和 Playwright 入口。
- 增加针对 CI 配置和命令漂移的轻量检查，或改用共享脚本减少重复命令。

### 不做

- 不修改业务状态机、账本或 Provider 行为。
- 不新增真实 RunPod、R2、Hatchet 或 GPU 集成。
- 不借机升级全部依赖。

### 验收

- 空 PostgreSQL 数据库可一次升级到唯一 head。
- API 测试与 Ruff 通过。
- Web Vitest、ESLint、Build 与 Playwright 通过。
- OpenAPI Client 生成后工作树无差异。
- Makefile 和项目文档中不再出现前端 `npm run`。

## 6.3 SIM-02：Provider Cancel Outbox 与崩溃恢复

### 目标

消除“取消状态已提交，但 Provider Cancel 尚未调用时进程崩溃”的丢失窗口。

### 范围

- 取消 API 在同一事务内写入 `CANCEL_REQUESTED` 和唯一 Cancel Outbox 事件。
- 独立 Cancel Dispatcher/Worker 领取、续租、重试并完成事件。
- Provider Cancel 使用稳定幂等键；重复 API、重复派发和租约过期重领不会产生重复副作用。
- Cancel 与成功输出、最终失败、迟到成功、迟到取消竞争时复用现有统一完成路径。
- 记录尝试次数、下次执行时间、最后错误和关联 Job/Attempt。

### 模拟验收场景

- API Commit 后、第一次派发前崩溃。
- Claim 后、Provider 调用前崩溃。
- Provider 已接受 Cancel、响应丢失。
- Provider Cancel 连续临时失败后成功。
- Cancel 与成功输出并发，最终只允许 Settle 或 Release 其中一种。
- 重复取消只产生一个有效业务结果。

### 不做

- 不调用真实 Provider Cancel API。
- 不把整个 Generation 生命周期重写为新架构。

### 验收

- 所有场景使用模拟 Provider 和 Fake Clock 稳定复现。
- 任意崩溃窗口后 Cancel 事件仍可被重新领取。
- Job、Attempt、Outbox 与账本结果一致，Settle/Release 各最多一次且互斥。

### 实现状态（2026-08-30）

- [x] 迁移 `0009_reliable_cancellation` 为 `outbox_events` 增加可空 `attempt_id`，并用
  `(attempt_id, job_id)` 外键绑定 Generation Attempt；启动 Outbox 兼容保留。
- [x] 取消 API 在同一事务提交 `CANCEL_REQUESTED`、唯一
  `provider.cancel.requested` Outbox 和 API 幂等结果；不再同步调用 Provider。
- [x] 独立 Cancel dispatcher 支持 `SKIP LOCKED` 领取、`lock_token` 条件更新、调用期间
  心跳续租、租约过期重领、定时重试、尝试次数和最后错误记录。
- [x] Provider Cancel 稳定键固定为 `attempt:{attempt_id}:cancel:v1`；接受后响应丢失、
  调用前后崩溃和重复派发均使用同一键恢复。
- [x] Cancel 结果复用统一 Provider 完成路径；成功／取消竞态测试证明最终只 Settle 或
  Release 一次，迟到结果不能改写既有终态。
- [x] 全部场景只使用 Fake Clock、Fake Provider、模拟 Storage 和故障注入；未调用真实
  Provider、RunPod 或 GPU，未进入 SIM-03。

## 6.4 SIM-03：Project 软删除与 Storage Cleanup Outbox

### 目标

删除项目时保留账本、Job、Attempt、事件和 Output 审计历史，并可靠清理可删除对象。

### 范围

- Project 增加软删除状态和时间；列表与详情默认过滤已删除项目。
- 有活动 Job 的项目拒绝删除，或先进入明确的删除等待状态。
- 删除事务只改变业务状态并写入唯一 Storage Cleanup Outbox，不直接递归删除业务历史。
- Cleanup Worker 使用模拟 Storage 删除素材、预览和允许删除的输出对象。
- 清理失败可重试，缺失对象视为幂等成功；记录每个对象的清理结果。
- 已删除项目禁止新增 Shot、Asset、Quote、Job 和 Batch。

### 模拟验收场景

- 重复删除同一项目。
- Storage 删除部分成功后崩溃并重试。
- 对象已经不存在。
- 清理过程中新增资源请求被拒绝。
- 删除后账本、Job、Attempt、Event 和 Output 元数据仍可供管理员审计。

### 不做

- 不接入真实对象存储。
- 不实现用户自助恢复站或复杂保留策略 UI。
- 不因软删除顺手拆分全部 ORM 文件。

### 验收

- 业务历史不被级联硬删除。
- 模拟 Storage 最终完成全部允许的对象清理。
- 重放 Cleanup 事件不会产生错误结果或破坏审计数据。

### 实现状态（2026-08-30）

- [x] 迁移 `0010_project_soft_delete` 为 Project 增加一致性约束保护的 `ACTIVE`／
  `DELETED` 状态和 `deleted_at`，并新增唯一 `storage_cleanup_events` 与逐对象结果表。
- [x] 删除 API 使用项目行锁，在同一事务提交软删除状态和
  `project:{project_id}:storage-cleanup:v1`；重复删除复用同一事件，活动 Job 返回
  `PROJECT_HAS_ACTIVE_JOBS`。
- [x] Project 列表／详情默认隐藏已删除项目；Shot、Asset、Quote、Generation 与 Batch
  写入口在 ACTIVE Project 行锁下校验，删除后的新增与修改请求被拒绝。
- [x] Cleanup dispatcher 支持 `SKIP LOCKED` 领取、租约过期重领、失败退避、缺失对象幂等、
  逐对象尝试／错误／完成记录，以及素材元数据的 `DELETED` 标记。
- [x] 定向测试覆盖审计历史保留、活动任务拒删、重复删除、部分清理失败、删除后崩溃、
  对象缺失重放和删除后禁止新增；只使用模拟 Storage 与 Fake Clock，未接入真实对象存储。

## 6.5 SIM-04：Reconciler、Dead Letter 与 Readiness

### 目标

让模拟控制面能够发现和处理卡住的任务、过期租约及永久失败事件，并能从健康检查中
区分“进程存活”和“系统可工作”。

### 实施状态（2026-08-30）

已完成。控制面 Reconciler 复用现有 Generation 执行与三类 Outbox dispatcher，恢复过期
Provider Event 租约并重新进入幂等业务入口，不直接修改任务终态或账本。Generation、Cancel
和 Storage Cleanup Outbox 统一使用可配置重试上限；永久失败后保留原事件、错误和尝试次数，
进入 `dead_letter_events`。受保护的 Admin API 支持死信查询、显式回放、操作审计查询和最小
运行指标；重复回放不会重复恢复事件或写入第二条审计。

`/healthz` 仅表示进程存活，`/readyz` 分别检查数据库连接、Alembic head、Storage 和工作流
启动能力，任一关键依赖失败即返回 503。后台 Reconciler 默认每 30 秒运行一次，可通过配置
调整或禁用。本 Issue 未接入外部告警平台，也未建立通用工作流系统。

### 范围

- Reconciler 扫描超时的 Job、Attempt、Provider Event、Cancel/Cleanup Outbox。
- 只通过现有幂等业务入口恢复，不直接绕过状态机修数据。
- Outbox 达到重试上限后进入 Dead Letter，保留错误、次数和原始事件。
- 提供受保护的 Admin 查询和显式重放能力；重放使用新的操作审计记录。
- `healthz` 只表示进程存活；新增 readiness 检查数据库、迁移状态、Storage 和工作流启动能力。
- 增加结构化日志和最小指标：待处理数、最老事件年龄、重试数、Dead Letter 数、卡住 Job 数。

### 模拟验收场景

- Outbox 和 Provider Event 租约持有者崩溃。
- Poll/Cancel/Cleanup 长时间无进展。
- 临时失败恢复，永久失败进入 Dead Letter。
- Dead Letter 人工重放后成功，重复重放保持幂等。
- 数据库或模拟 Storage 不可用时 readiness 失败，恢复后自动变为成功。

### 不做

- 不接入外部告警平台或商业可观测性服务。
- 不建设通用工作流平台。

### 验收

- Reconciler 可重复执行且不会重复提交、发布、结算或返还。
- Dead Letter 可查询、可审计、可显式重放。
- 健康检查不会在关键依赖不可用时错误报告 Ready。

## 6.6 SIM-05：模拟故障注入总验收与文档收口

### 目标

用完全离线、确定性的模拟链路证明控制面不变量，并明确真实能力边界。

### 实施状态（2026-08-30）

实现、故障矩阵、Runbook 和验收报告已收口；离线 API 结果为
183 passed、1 skipped、1 deselected，Ruff、仓库命令基线、Worker contract、Vitest、
ESLint、Typecheck、Build 和 OpenAPI Client 生成均通过。

PostgreSQL 空库迁移到唯一 Alembic head 已通过，并发账本回归连续 3 轮通过；Playwright
完整业务闭环通过（1 passed）。当前状态为 `SIMULATION_ACCEPTED`，真实路线继续
`CONDITIONAL`／默认禁用。

### 范围

- 汇总 Submit、Poll、Cancel、Retry、Finalize、Ledger、Project 和 Storage 故障矩阵。
- 所有时间相关测试使用 Fake Clock，不使用真实长时间 `sleep`。
- 所有跨系统操作使用模拟 Provider/Storage 和可编程 Fault Injector。
- 运行 PostgreSQL 并发回归、API 测试、Ruff、前端测试、Lint、Build 和 Playwright。
- 更新 README、阶段文档、Runbook 和模拟验收报告。
- 报告明确列出未验证项：真实 GPU、真实 RunPod、真实网络、真实对象存储、真实成本和真实性能。

### 不做

- 不申请或使用真实 RunPod 凭据。
- 不运行真实 GPU、真实 Worker 或付费 Benchmark。
- 不把模拟数据写成真实性能或成本结论。

### 验收

- 模拟链路覆盖提交结果未知、轮询恢复、重复/乱序事件、损坏媒体、重试、取消、软删除、
  清理、Dead Letter 和并发账本场景。
- 每种终局下只发布一个最终 Output，并且 Settle/Release 互斥且各最多一次。
- 全部门禁实际通过并记录命令与结果。
- 最终状态标记为 `SIMULATION_ACCEPTED`，真实路线仍保持 `CONDITIONAL`/禁用。

## 6.7 每个 PR 的强制说明模板

```text
范围：本 PR 只解决什么
不做：明确不顺手修改什么
行为变化：Public Contract / DB / 状态机是否改变
依赖变化：依赖方向如何改善
事务边界：本 PR 的原子操作是什么
测试：新增和运行了哪些测试
兼容层：是否保留旧入口，删除 Issue 和截止点是什么
风险：已知限制和回滚方式
```

---

# 7. 数据库迁移建议顺序

当前仓库已经存在 `0008_attempt_provenance`。后续迁移不得复用已经占用的编号，
实施时必须先读取实际 Alembic head，再按以下业务顺序分配新 revision：

```text
reliable_cancellation     # SIM-02：Cancel Outbox、租约、重试和错误字段
project_soft_delete       # SIM-03：软删除状态和时间
storage_cleanup_outbox    # SIM-03：可靠对象清理事件
outbox_dead_letter        # SIM-04：Dead Letter 和重放审计
```

当前模拟 Provider 继续作为进程内确定性测试适配器，不为它新增持久化表。只有未来明确要把
模拟 Provider 部署成可独立重启的开发服务时，才另立 Issue 设计其持久化状态。

迁移要求：

1. 不修改历史迁移。
2. 每个迁移支持空库升级。
3. 当前阶段数据库可连续升级。
4. 删除字段必须采用 expand → migrate → contract。
5. 本轮不以删除 `mock_mode` 为前置目标；若后续删除，仍须先停止写入，再迁移数据，最后收缩字段。

---

# 8. 最终故障注入验收矩阵

## Submit

- Submit 成功但 API 响应丢失；
- Submit 未知但 Provider 实际创建任务；
- 重复 Submit；
- Submit 前进程崩溃；
- Submit 后记录 Provider Job ID 前崩溃。

## Poll / Webhook

- 无 Webhook；
- 重复 Webhook；
- 乱序 Webhook；
- 迟到旧 Attempt Webhook；
- Poll 与 Webhook 并发；
- Webhook 处理 Commit 后响应丢失；
- 相同 Event ID 不同 payload。

## Output

- 正确视频；
- `ftyp` 存在但损坏；
- 错误 codec；
- 错误尺寸；
- 错误时长；
- SHA 不一致；
- Storage stat/open 临时失败；
- Finalizer 并发；
- Finalizer Commit 前后崩溃。

## Retry

- Retryable Failure 后成功；
- 连续 Retryable Failure 到达上限；
- Retry Attempt 创建后 Worker 崩溃；
- 新 Attempt 与旧 Webhook 竞争。

## Cancel

- Cancel 请求 Commit 后崩溃；
- Cancel 调用异常后重试；
- Cancel accepted 但未确认；
- Cancel 与成功输出竞争；
- 重复取消。

## Ledger

- Reserve 并发超扣；
- Settle 重放；
- Release 重放；
- Settle 与 Release 并发；
- Batch 部分成功；
- 终态后迟到事件。

## Project / Storage

- 活动任务项目删除；
- 重复删除；
- Storage 删除失败；
- 删除后新增 Shot/Asset；
- 历史 Job/Event/Ledger 保留。

---

# 9. 阶段最终完成定义

只有同时满足以下条件，才可宣布本轮改进完成：

- [ ] CI 从空 PostgreSQL 数据库升级到当前唯一 Alembic head，仓库命令统一使用 pnpm；
- [ ] 模拟 Provider、模拟 Storage、Fake Clock 和 Fault Injector 可确定性复现所有关键异步场景；
- [ ] 无 Webhook 时任务可通过 Poll 最终完成；
- [ ] Webhook Retryable Failure 后新 Attempt 会自动执行；
- [ ] Provider 成功但媒体损坏时不会结算；
- [ ] Cancel 请求与 Cancel Outbox 在同一事务提交，任意崩溃窗口后仍可恢复；
- [ ] Settle 与 Release 永远互斥且各最多一次；
- [ ] 项目删除不再硬删除账本和任务历史；
- [ ] Storage Cleanup 可重试，部分成功和对象缺失均保持幂等；
- [ ] Public API 不暴露模拟和 Provider 内部字段；
- [ ] Asset 通过短期 Claim 进入模拟 Worker；
- [ ] Tier 绑定不可变 Route Version，未实现能力不可售卖；
- [x] Reconciler 可恢复过期租约和卡住任务，不绕过状态机修改结果；
- [x] 永久失败事件进入可查询、可审计、可显式重放的 Dead Letter；
- [x] Readiness 在数据库、Storage 或工作流能力不可用时正确失败；
- [ ] PostgreSQL 并发、故障注入、Pytest、Ruff、Vitest、ESLint、Typecheck、Build、Playwright 全部通过；
- [ ] Poll、Webhook、Retry、Cancel 和 Finalize 继续复用统一幂等完成规则；
- [ ] README、阶段文档、Runbook 与模拟验收报告和实际实现一致；
- [ ] 仓库只使用 pnpm 和唯一锁文件，Makefile、CI 和开发文档命令一致。

完成后，本轮状态只能标记为 `SIMULATION_ACCEPTED`：它证明模拟控制面的事务、幂等、
恢复、媒体校验和生命周期规则通过离线验收，不证明真实 RunPod、真实 GPU、真实视频质量、
真实性能、真实网络、真实对象存储或真实成本。真实路线继续保持 `CONDITIONAL`/禁用。

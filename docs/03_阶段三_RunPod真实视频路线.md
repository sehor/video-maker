# 03｜阶段三：RunPod 第一条真实视频路线

> 目标：只上线一条稳定、可追溯的 720p 开放模型路线。  
> 前置：阶段二的账本、Outbox、Hatchet、幂等和恢复测试全部通过。

开发环境正在按 [Windows 原生开发环境改造计划](Windows原生开发环境改造计划.md)
迁移。WINDEV-01/02 已加入工作流 Backend 选择、生产保护和随 API 生命周期
运行的开发专用 Local Runner，业务执行与账本语义不变；现有集成栈显式使用
`WORKFLOW_BACKEND=hatchet`。WINDEV-03 已提供 `scripts/dev.ps1` 原生命令并通过
隔离库迁移和浏览器闭环验证；用户授权后的开发库迁移与原生启动验证也已通过，
完整环境总验收仍未完成。

## 1. 交付范围

- RunPod Serverless Endpoint；
- 基于 `worker-comfyui` 的固定视频 Worker；
- 一条 Wan 720p workflow；
- RunPod Adapter：submit、poll、webhook、cancel、成本读取；
- R2 输入／输出、媒体校验、不可变版本和 Benchmark；
- route kill switch。

本阶段不同时接 Vast、商业 API、第二模型或 1080p。

## 2. 先做 POC

`worker-comfyui` 只视为基础 Worker，先验证：

1. 固定 Wan workflow 可在目标 GPU 运行；
2. MP4 能被正确定位、收集并上传，而非仅处理图片输出；
3. 异步 `/run`、状态查询、回调和错误返回可用；
4. 5 秒、9:16 与 16:9 均能完成；
5. 显存、冷启动、运行时间和镜像大小可接受；
6. 临时文件会清理。

POC 结论应明确为“直接配置”“薄适配”或“改用自有薄 Worker”。尽量只改 handler、产物收集和上传层，不改 ComfyUI 核心。

## 3. 首条路线

```text
route: fast_wan_i2v_720_v1
provider: RunPod Serverless
input: 单张参考图 + prompt
resolution: 720p
duration: 5s；10s 在 Benchmark 后再开
aspect: 9:16 / 16:9
```

文生视频、多图、首尾帧、LoRA、视频转视频和 1080p 后置。

## 4. Worker 与 workflow

镜像至少包含：

```text
worker-comfyui pinned commit
ComfyUI pinned release/commit
approved custom nodes + nodes.lock
FFmpeg / ffprobe
fixed workflow
model manifest + SHA-256
output adapter
license notices + SBOM
```

规则：不使用 `latest`；记录镜像 digest；生产不装 ComfyUI Manager；ComfyUI 不暴露公网；模型、节点和 workflow 均不可变版本化。

Control Plane 只传白名单字段：`job_id`、`attempt_id`、`workflow_id`、prompt、系统签发的参考素材声明、时长、画幅、输出声明和 callback 声明。禁止任意 workflow JSON、节点参数、模型路径、Python、下载 URL 和上传目标。

## 5. RunPod Adapter

- 使用异步任务接口；
- `attempt_id` 是业务幂等标识；
- 保存 Provider Job ID 后才进入 `SUBMITTED`；
- submit 超时不能直接判断未提交，必须查询或对账；
- webhook 先验签，polling 作为补偿，两者调用同一幂等完成函数；
- 回调可重复、乱序和迟到；
- cancel 只作 best effort；取消请求与唯一 Cancel Outbox 同事务提交，后台使用
  `attempt:{attempt_id}:cancel:v1` 重放；确认无有效输出后才返还。

首条 Adapter 固定调用 `https://api.runpod.ai/v2/{endpoint_id}`，不接受可配置 origin 或用户 URL。
RunPod 文档化 webhook 没有可供 Control Plane 验证的密码学签名，因此在签名能力得到独立验证前
保持关闭，仅使用 polling；禁止为了形式上满足 webhook 而信任 unsigned callback。

Worker 发布由 `scripts/release_worker.py` fail-closed 门禁：OCI 发布镜像与回滚镜像均须用 registry
实际可验证的 digest，附 SPDX SBOM、许可证通知、固定模型／节点登记、workflow hash、fixture smoke
与真实 GPU Benchmark 证据。未执行真实 POC 时只允许保留 `BLOCKED_UNVALIDATED` 模板，路线不得启用。

统一错误至少覆盖鉴权、容量、5xx、队列超时、启动失败、模型加载、OOM、workflow 失败、素材下载、上传失败、缺失／损坏媒体和未知错误。是否重试由 Control Plane 决定。

## 6. 输出与媒体校验

Worker 返回对象 key、大小、SHA-256、GPU、队列、冷启动、运行时间、计费时间、Worker 和 workflow 版本。Worker 返回成功不等于 Job 成功。

Control Plane 必须：

1. 校验对象存在、大小和 SHA-256；
2. 用 `ffprobe` 检查容器、流、时长、尺寸、帧率和编码；
3. 用 FFmpeg 完整解码检查；
4. 验证为 MP4／H.264／yuv420p、1280×720 或 720×1280；
5. 生成预览图并写 Output；
6. 原子发布唯一最终 Output，再结算秒数。

损坏、空视频、错误尺寸、不可解码或时长严重不符均视为 Attempt 失败。允许把 720-class 原生尺寸 crop／pad 到规范尺寸，禁止放大后冒充 1080p。

## 7. 安全与成本

- Worker 只能下载系统允许的私有对象，限制协议、域名、重定向和大小，防止 SSRF；
- Endpoint、callback 和 R2 凭据按环境隔离，不写日志；
- 输出默认私有，任务结束清理临时文件；
- Route 可立即停止接单；
- 每个 Attempt 记录 Provider Job ID、GPU、队列／冷启动／运行／计费时间、成本和成本来源；
- 官方成本不可得时可估算，但必须标记 `ESTIMATE`。

## 8. Benchmark

固定数据集覆盖人物特写、双人场景、动作、夜景、动漫／写实、9:16／16:9，并至少重复三次。记录成功率、OOM、p50／p95 队列／冷启动／运行、每成功输出秒成本、人工质量与一致性评分，以及 Worker digest、workflow 和模型哈希。

同名 GPU 在不同环境需分别 Benchmark。

## 9. 实施顺序

1. 独立 POC；
2. 锁定 workflow、模型和输入范围；
3. 构建不可变 Worker；
4. 建立 Endpoint 和 Adapter contract tests；
5. 接入 Hatchet Attempt 流程；
6. 完成 R2、媒体校验、错误和取消；
7. Benchmark；
8. 内部小流量灰度和成本核对。

## 10. 验收门槛

- [ ] 5 秒 720p、9:16／16:9 稳定成功；
- [ ] 用户不能提交任意 workflow、模型、节点或 URL；
- [ ] webhook／polling 重放不会重复完成或结算；
- [ ] Worker 成功但媒体损坏时不会结算；
- [ ] 中断后任务可恢复或正确返还；
- [ ] 每个输出可追溯到 Attempt、Worker、workflow 和模型哈希；
- [ ] Benchmark 和成本记录完整；
- [ ] ComfyUI 不暴露公网；
- [ ] 系统仍没有可售 1080p、2K 或 4K 路线。

## 11. worker-comfyui POC 基线

Issue #14 固定使用 worker-comfyui `5.8.7`（commit
`a1981e99b1f5a7201f387653420ad1f275b97d0a`）、ComfyUI `v0.29.0`（commit
`a8c44f9b2a0678ac4082e3529a3f43db7472acfe`）和隔离的 comfy-cli `v1.18.0`。
系统只接受仓库内的 `fast_wan_i2v_720_v1` workflow 与 claim-only Worker Contract；
不接受 workflow JSON、节点、模型、代码或任意 URL。

在独立 RunPod 凭据和成本授权前，该 Worker 保持 `CONDITIONAL`，不得启用路线。
镜像 digest、模型 SHA-256、5 秒横竖屏真实输出、FFmpeg 校验、冷启动、运行时间、成本、
失败记录和继续／拒绝结论必须在获批的真实 POC 后补齐。

Issue #14 收尾时仍没有 RunPod 凭据和费用授权，用户明确选择跳过真实付费 POC 并继续
后续工作。该选择不是 POC 通过：GPU、冷启动、运行时间、真实输出和成本均未验证，
worker-comfyui 继续保持 `CONDITIONAL`，路线不得启用。

## 12. Issue 8 最终模拟验收状态

Issue 8 只完成离线、确定性的 Control Plane 模拟验收，不代表真实 RunPod POC 通过。
最终模拟矩阵覆盖：

| 场景 | 模拟验收结果 | 幂等与恢复断言 |
|---|---|---|
| 排队 → 运行 → 成功 | 通过 | 进程重启后继续；只提交、发布和结算一次 |
| Provider 最终失败 | 通过 | 不发布；只返还一次 |
| Provider 超时／轮询预算耗尽 | 通过 | 重试有界；不重复提交同一 Attempt；只返还一次 |
| 取消 | 通过 | best-effort 取消确认后终止；不发布；只返还一次 |
| submit outcome unknown | 通过 | 重启后以 polling 对账，不重新 submit；最终只发布、结算一次 |
| polling／Hatchet 进程重启恢复 | 通过 | 每个 polling step 均可从 PostgreSQL 状态恢复 |
| 重复执行、重复 webhook、poll/webhook 竞争 | 通过 | 单一完成路径；只发布、结算或返还一次 |
| 迟到成功／迟到取消 | 通过 | 已有终态不被旧事件改写；有效迟到成功只完成一次 |

模拟验收使用 `DeterministicRunPodSimulator`、受控时钟、故障注入和私有对象存储契约；
真实 RunPod、真实 R2、真实 Hatchet／容器服务在本验收中均未启用。

截至 Issue 8 收尾，真实 RunPod POC **仍未通过**：没有验证真实 GPU、镜像启动、
真实 5 秒 720p 横竖屏输出、冷启动、运行时间、真实 R2 传输或供应商成本。
`worker-comfyui` 和真实 RunPod 路线继续保持 `CONDITIONAL`／默认禁用；只有完成独立、
获批且有凭据的真实 POC 后，才可更新本节结论或启用路线。模拟通过不得用于对外宣称
真实 POC、Benchmark 或生产就绪。

## 13. SIM-02 可靠取消模拟验收状态

Provider 提交后的取消已改为独立 Cancel Outbox：API 只在 PostgreSQL 同一事务中提交
`CANCEL_REQUESTED`、API 幂等结果和绑定 Job／Attempt 的唯一取消事件；后台 dispatcher
负责领取、租约心跳、临时失败退避、租约过期重领和完成标记。Provider Cancel 始终使用
`attempt:{attempt_id}:cancel:v1`，因此调用已接受但响应丢失、进程在调用前后崩溃或重复
派发时可以安全重放。

离线测试使用 Fake Clock、Fake Provider 和故障注入，覆盖提交后派发前崩溃、Claim 后
崩溃、接受后响应丢失、连续临时失败、重复派发，以及取消与成功输出双向竞态。取消确认、
成功、最终失败和迟到事件继续进入统一完成路径；每个 Job 只允许 Settle 或 Release 之一，
且各最多一次。本节仍只表示模拟控制面验收，不调用真实 Provider、RunPod 或 GPU，也不
改变真实路线的 `CONDITIONAL`／默认禁用状态。

## 14. SIM-03 项目软删除与 Storage Cleanup 模拟验收状态

Project 删除已改为软删除：API 在项目行锁下检查活动 Job，并在 PostgreSQL 同一事务中
提交 `DELETED`、`deleted_at` 和唯一的
`project:{project_id}:storage-cleanup:v1` 事件。列表与详情默认隐藏已删除项目，所有新增
Shot、Asset、Quote、Generation 和 Batch 的入口复用 ACTIVE Project 检查；重复删除保持
204 幂等，Job、Attempt、Event、Output 和账本历史不会随项目删除而移除。Public Project
Schema 不暴露内部删除状态。

Storage Cleanup dispatcher 逐对象保存素材／输出的删除尝试、错误和完成时间，支持领取
竞争、租约过期重领、临时失败退避、部分成功后继续以及对象已缺失的幂等成功。离线测试
使用 Fake Clock、模拟 Storage 与崩溃注入，覆盖删除前后崩溃和重复派发；未接入真实 R2、
RunPod、GPU 或付费服务，也不改变真实路线的 `CONDITIONAL`／默认禁用状态。

## 15. SIM-04 控制面恢复、死信与就绪状态

模拟控制面新增定时 Reconciler：它扫描过期 Provider Event 租约和长时间无进展的
Job／Attempt，并只调用现有幂等 Generation 执行入口及 Generation、Cancel、Storage Cleanup
dispatcher，不直接写任务终态、发布结果或账本。重复执行仍受稳定幂等键、Attempt 轮询账本
和统一完成路径保护。

三类 Outbox 达到配置的最大尝试次数后进入 `DEAD_LETTER`，同时在
`dead_letter_events` 保存来源、原始事件、错误和尝试次数。受 Admin 权限保护的接口提供查询、
显式回放、回放审计查询及待处理数、最老事件年龄、重试数、死信数和卡住 Job 数；回放不允许
提交替换 payload，重复回放保持幂等。

`/healthz` 继续只表示 API 进程存活；`/readyz` 检查数据库连接、Alembic 迁移 head、Storage
和工作流启动能力，任何关键检查失败均返回 503。本节仍是离线模拟控制面验收，不代表真实
RunPod、真实对象存储或真实 Hatchet 服务已经生产就绪。

## 16. SIM-05 模拟故障注入总验收状态

离线、确定性的控制面总验收已完成，当前状态为 `SIMULATION_ACCEPTED`。验收矩阵汇总 Submit、Poll、
Webhook、Retry、Cancel、Finalize、Ledger、Project、Storage、Dead Letter 与 Readiness，
并验证每个 Job 最多发布一个最终 Output，`SETTLE`／`RELEASE` 互斥且各最多一次。
时间相关生命周期测试使用 Fake Clock；跨系统故障使用模拟 Provider／Storage、
`FaultInjector` 和媒体 fixture。

PostgreSQL 从空库迁移到唯一 Alembic head `0011_control_plane_recovery` 已通过；并发账本回归
连续 3 轮通过；Playwright 完整业务闭环通过（1 passed）。Docker 验收复用了现有镜像和数据卷，
未构建或拉取镜像。

完整矩阵、自动化测试映射与门禁记录见
`reports/SIM-05_模拟故障注入验收报告.md`，故障恢复步骤见
`runbooks/模拟控制面故障恢复.md`。

本状态不验证真实 GPU、RunPod、网络、对象存储、Hatchet 服务、视频质量、性能或成本。
真实 RunPod／worker-comfyui 路线仍为 `CONDITIONAL`／默认禁用，不能据此启用或对外宣称
真实 POC、Benchmark 或生产就绪。

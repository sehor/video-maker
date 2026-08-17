# 03｜阶段三：RunPod 第一条真实视频路线

> 目标：只上线一条稳定、可追溯的 720p 开放模型路线。  
> 前置：阶段二的账本、Outbox、Hatchet、幂等和恢复测试全部通过。

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
- cancel 只作 best effort，确认无有效输出后才返还。

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

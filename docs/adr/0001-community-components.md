# ADR 0001：社区组件采用边界

- 状态：已接受
- 日期：2026-08-25
- 适用阶段：阶段二至阶段三

## 背景

阶段一使用 FastAPI `BackgroundTasks`、Mock Provider 和本地文件完成了业务闭环。进入可靠任务和真实视频阶段前，需要避免继续自研持久任务编排、ComfyUI 安装管理和媒体探测基础设施，同时保证 PostgreSQL 仍是 Job、Attempt 和账本的唯一业务真相。

## 决策

| 组件 | 决策 | 边界 |
|---|---|---|
| Hatchet OSS／`hatchet-sdk` | 阶段二采用 | 只负责编排、等待、任务级重试和恢复；所有业务状态与幂等约束仍写入 PostgreSQL |
| FFmpeg／ffprobe | 采用 | 用于媒体探测、校验和受控转码；禁止 shell 拼接，必须设置超时并记录构建配置 |
| worker-comfyui | POC 通过后采用 | 只作为 RunPod／ComfyUI 基础 Worker；自有 handler 保持薄层，只执行固定 workflow |
| comfy-cli | 仅构建与开发采用 | 用于安装、快照和回归；不进入在线 Job 执行链路，生产不允许运行时安装节点或模型 |
| ComfyUI | 阶段三条件采用 | 固定版本和 workflow，不开放 UI，不允许用户提交 workflow、节点、模型或任意 URL |
| ComfyUI Manager | 生产拒绝 | 不进入生产镜像；隔离 POC 也不得成为发布流程依赖 |
| Temporal、Celery、Dramatiq | 仅借鉴 | 借鉴 activity/task 分离、幂等、确认和重试语义；不引入第二套编排系统 |
| Airflow、Prefect、Dagster | 拒绝 | 不用数据工作流平台替代在线业务任务编排 |
| 多云调度、国外账单平台、通用插件系统 | 拒绝 | 当前阶段不建抽象，也不预留虚假接口 |

支付继续后置；未来只考虑微信支付、支付宝等国内渠道，本 ADR 不设计支付接口。

## 版本和发布规则

1. 生产依赖、镜像、ComfyUI、custom node、模型和 workflow 禁止使用 `latest`、未锁定 branch 或可变标签。
2. Hatchet Server 与 Python SDK 必须作为兼容组合验证并共同锁定，不能只升级其中一端。
3. worker-comfyui 锁定 release、commit 和镜像 digest；ComfyUI 以该 Worker 验证过的 commit 为准，不独立追最新版本。
4. comfy-cli 只参与构建和回归。若无法稳定复现节点、模型和 ComfyUI 版本，则改用显式 Dockerfile 和 lock manifest。
5. FFmpeg 必须记录版本、`-buildconf`、启用的 codec/library 以及最终适用的 LGPL/GPL 条款。
6. 每次新增或升级依赖都同步更新 `docs/licenses/` 台账；Worker 发布同时生成 SBOM、通知文件和回滚记录。

## 初始评估基线

基线只用于后续 POC，不等于已进入生产：

- Hatchet OSS `v0.101.27`；`hatchet-sdk` `1.37.2`；
- worker-comfyui `5.8.7`；
- comfy-cli `v1.18.0`；
- ComfyUI 由 worker-comfyui POC 选择兼容 commit；
- FFmpeg 由运行镜像锁定具体发行版和构建参数。

## 后果

- 需要运行 Hatchet 服务并维护 Outbox dispatcher，但不再自研队列、工作流历史和恢复控制面。
- 强 copyleft 组件保持在 Worker／构建边界，并单独记录源码、修改和通知义务。
- 真实 RunPod POC 必须独立授权凭据和成本；POC 未通过前，worker-comfyui 保持条件采用。
- 本决策不整体替换现有项目，也不改变阶段一产品接口。

## 参考

- Hatchet：https://github.com/hatchet-dev/hatchet
- worker-comfyui：https://github.com/runpod-workers/worker-comfyui
- comfy-cli：https://github.com/Comfy-Org/comfy-cli
- ComfyUI：https://github.com/Comfy-Org/ComfyUI
- FFmpeg：https://ffmpeg.org/legal.html

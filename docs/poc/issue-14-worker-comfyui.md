# Issue #14｜worker-comfyui 固定 Workflow POC

## 范围

本 Issue 只锁定 worker-comfyui 5.8.7、兼容的 ComfyUI commit、comfy-cli 1.18.0、
固定 5 秒 720p Wan I2V workflow、Worker Contract fixture，以及无需 GPU 的回归门禁。

不实现 RunPod Adapter、第二模型、1080p、多供应商、用户自定义 workflow／节点／模型、
任意代码或任意 URL。后续 Issue 才能把通过 POC 的固定 Worker 接入 Control Plane。

## 官方版本证据

| 组件 | 官方 tag | commit |
| --- | --- | --- |
| runpod-workers/worker-comfyui | `5.8.7` | `a1981e99b1f5a7201f387653420ad1f275b97d0a` |
| comfyanonymous/ComfyUI | `v0.29.0` | `a8c44f9b2a0678ac4082e3529a3f43db7472acfe` |
| Comfy-Org/comfy-cli | `v1.18.0` | `6a5e9d772453f27b0778b14e8a4b1e25ac5f949e` |

worker-comfyui 5.8.7 的官方 Dockerfile 默认安装 ComfyUI 0.29.0，因此把该 tag 的 commit
作为兼容基线。官方 registry 中没有对应的
`docker.io/runpod/worker-comfyui:5.8.7[-base]` manifest，因此没有猜测其他镜像标签；
真实 POC 必须从该 commit 构建、写入隔离 registry、记录 digest 并验证。

## 本地门禁

- production stage 删除 ComfyUI Manager，且不复制隔离的 comfy-cli 构建环境；
- ComfyUI 仅由 Worker 容器内部访问，不声明 8188 公网端口；
- workflow、模型文件名、节点和系统参数绑定均在仓库内固定；
- 请求严格拒绝额外字段，只接受系统签发的 input/output/callback claim；
- 固定输出必须由 Control Plane 的 R10 FFmpeg／ffprobe 层再次验证；Worker 的成功响应
  不能直接触发结算。

## 真实 POC 门禁与记录要求

当前状态为 `BLOCKED_PENDING_CREDENTIAL_AND_COST_AUTHORIZATION`，worker-comfyui 仍为
`CONDITIONAL`。获得独立 RunPod 凭据和明确成本上限后，必须补齐：

1. 不可变镜像 digest 和全部模型 SHA-256；
2. 5 秒 16:9 与 9:16 的真实生成，输出须通过现有 FFmpeg 媒体校验；
3. MP4 定位、claim 下载／上传、临时文件清理、异步状态和错误返回；
4. 冷启动、运行、计费时间、成本、GPU、失败详情；
5. “直接配置”“薄适配”或“改用自有薄 Worker”的明确结论。

未完成以上真实证据前，不得把 `poc-baseline.json` 改成通过，也不得启用或售卖路线。

## 一键 POC、费用和回滚

`scripts/run_worker_comfyui_poc.py` 是唯一 POC 入口。`preflight` 只返回凭据、费用上限、
镜像和 registry 认证是否存在，不输出值。`execute` 必须显式传入
`ISSUE-14-PAID-POC` 确认，并要求独立 Endpoint、不可变镜像 digest、保守的全包每秒费率、
私有 claim fixture、模型哈希和私有媒体目录。

两种画幅严格串行。每次 submit 前按剩余费用和剩余用例计算执行窗口，扣除取消缓冲，
同时设置 RunPod `policy.executionTimeout`；本地轮询达到窗口时再次 cancel。费用不足时不再
提交，任意失败都会在 `finally` 中取消仍活动的任务。中断后可用同一证据文件执行
`cleanup`，且取消失败会保留 `active_jobs_remaining`，禁止误判通过。

证据写入用户指定的私有仓库外路径，采用临时文件加原子替换。模板位于
`workers/runpod-comfyui/evidence-template.json`，记录供应链 digest、横竖屏运行、
FFmpeg 完整解码、冷启动／运行／计费时间、费用、失败、清理和继续／拒绝结论；不记录
API key 或任何 claim。

## Issue 验收状态

- [x] 上游 tag／commit、固定 workflow、comfy-cli 和不可变镜像输入规则已锁定；
- [x] production stage 删除 ComfyUI Manager，不暴露 8188，且只接受 digest 基础镜像；
- [x] contract 拒绝任意 workflow、节点、模型、代码和 URL；
- [x] input/output/callback 只接受系统签发 claim，合成 fixture 禁止用于付费 POC；
- [x] FFmpeg／ffprobe 完整解码、费用硬上限、超限取消、失败回滚和证据模板已有免费回归；
- [ ] 使用独立凭据在真实 RunPod 完成横竖屏生成，并填写镜像／模型 hash、时间、费用、
  失败和继续／拒绝结论。该项因缺少凭据／费用授权且用户明确选择跳过而未执行。

因此，真实 RunPod POC 是唯一未满足的 Issue 验收项。本 Issue 按用户授权以
`SKIPPED_BY_USER_UNVALIDATED` 收尾，不代表 POC 通过；GPU、冷启动、运行时间、真实成本、
真实横竖屏输出和继续／拒绝结论均未验证，worker-comfyui 仍为 `CONDITIONAL`。

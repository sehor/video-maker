# WINDEV-04 Hatchet Cloud 集成验证

日期：2026-08-31。分支：`codex/windows-native-development`。
前置提交：`09afc63`。范围仅为 WINDEV-04；不包含 WINDEV-05/06 或真实 RunPod。

状态：实现与离线验证完成；**真实 Cloud 验收未执行，不标记 ACCEPTED**。
本机没有配置 Cloud Token/Token 文件，不使用伪造 Token 充当远程通过结果。

## 改动

- Hatchet 默认 TLS 开启，地址默认从 Token 读取；支持显式 Host、HTTPS Server URL、
  命名空间、CA 文件和证书服务器名。拒绝未知策略、远程明文、生产明文和非法 URL；
  SDK 配置错误不会回显 Token。Local 导入不加载 SDK，默认开发无 Cloud 依赖。
- Windows Worker 对锁定 SDK 1.38.1 的 SIGQUIT 和事件循环信号问题做限定适配，
  listener 内设置 Windows 兼容策略；启动异常明确退出，旧引擎不走未适配的 legacy 路径。
  Linux Worker 工厂保持不变。没有修改依赖、SDK 安装目录或业务工作流定义。
- `scripts/dev.ps1 test-hatchet` 使用既有独立测试库、每次唯一 Cloud 命名空间和独立
  Windows Worker。普通测试跳过 Cloud；无凭据直接 SKIP；坏配置不回退 Local。
- `.env.example` 添加注释模板；Compose API/Worker 显式保留 `TLS_STRATEGY=none`，
  避免新默认值破坏已有本地集成。这两项配置调整不代表完成 Compose/CI 重构。
- 新增[操作手册](../runbooks/HatchetCloud_Windows集成.md)，说明凭据保存、轮换、撤销、
  地址排错、测试数据和 SDK 兼容边界。实际 `.env`、数据库账号密码均未修改。

## 验证记录

| 检查 | 结果 |
|---|---|
| PostgreSQL API 全量初跑 | 265 passed、3 skipped、1 failed，1224.22 秒；唯一失败为下述旧健康检查假设 |
| 修复后同库定向复测：健康检查 + TLS/Windows + 原生命令 | 49 passed，13.16 秒；包含初跑的唯一失败项 |
| TLS/Windows 与原生命令独立离线测试 | 48 passed，包括启动异常退出和子进程信号策略 |
| Ruff：API app/tests 与 scripts/dev.py | 通过 |
| 仓库基线检查 | 通过：单一 pnpm 锁文件、无 npm/npx 命令 |
| `./scripts/dev.ps1 test-hatchet` 无凭据路径 | 输出 `SKIP Hatchet Cloud`，退出 0；没有云连接或数据库操作 |
| 真实 Cloud API 提交、Windows Worker 消费、重复提交复用 | 未执行：没有开发租户 Token |

离线测试使用合成 Token，验证地址解析与覆盖、TLS 通道选择、配置错误脱敏、
Windows SDK listener 构造和信号处理、子进程策略隔离、启动失败退出，以及原生命令防护。
它们不验证真实 TLS 握手、Cloud 权限、心跳、远程耐久执行或 SDK 与当前云引擎兼容性。

全量回归中发现旧健康检查测试假设“测试库没有迁移版本”；既有原生测试库实际已迁移。
测试已改为显式令 Workflow 就绪检查失败，再断言 `/healthz` 存活、`/readyz` 返回 503；
不再依赖残留迁移状态，未改生产健康检查逻辑。全量回归的建表阶段有 PostgreSQL
`DataFileImmediateSync` 等待，无数据库锁阻塞；未调整数据库持久化或服务参数。
使用既有 `video-maker_windev03_test`；修复后未重复耗时 20 分钟的整套回归，
已对唯一失败项及本次相关用例重新验证。命令如下：

```powershell
./scripts/dev.ps1 test-api --disable-warnings
./scripts/dev.ps1 test-api tests/test_control_plane.py::test_health_is_live_when_readiness_fails tests/test_hatchet_tls.py tests/test_dev_commands.py --disable-warnings
uv run --project apps/api --no-sync ruff check apps/api/app apps/api/tests scripts/dev.py
uv run --project apps/api --no-sync python scripts/check_repository_baseline.py
./scripts/dev.ps1 test-hatchet
```

远程用例预期检查 API → Outbox → Cloud durable workflow → provider child task 完成，
Job 成功、单一 MP4、一次 SETTLE、相同幂等键复用 run ID。仅在该用例真实通过后补录
命令与结果，才能将 WINDEV-04 的远程验收标为通过。

## 风险与回滚

Windows 适配引用锁定 SDK 的内部 Worker 方法；升级依赖后须重跑本机与远程门禁。
独立命名空间只隔离名称，不提供独立租户权限；Cloud 测试会留下运行历史并可能产生用量。
配置凭据应使用开发租户。SDK 日志仅保存在忽略提交的本机临时目录，分享前须脱敏。

日常开发可直接恢复 `WORKFLOW_BACKEND=local`；Cloud 凭据在服务端撤销后再移除本地副本。
代码回滚以 `09afc63` 为前置参考，采用新 revert 提交，不回退数据库或重置现有用户工作。

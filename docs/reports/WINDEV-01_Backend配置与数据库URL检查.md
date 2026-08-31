# WINDEV-01｜Backend 配置与数据库 URL 检查

日期：2026-08-31。分支：`codex/windows-native-development`。

## 范围与回滚点

本次仅实施改造计划中的 WINDEV-01，并按用户补充要求核对新数据库 URL。
任务开始前的未提交修改已单独保存为 `ad180e2`；本次工作不改动已有账本、
取消、重试、Project 清理及 Control Plane 恢复语义。

本地 Runner、Cloud TLS、Windows 命令入口、CI 分离和 WSL 关闭状态总验收
仍属于 WINDEV-02～06，不能以本次验收替代。

## 已实现

- `WORKFLOW_BACKEND` 仅允许 `local`、`hatchet`，开发默认值为 `local`。
- `production + local` 在配置加载时拒绝；Hatchet 缺失或不可读 Token 明确失败。
- API 通过 `create_workflow_starter(settings)` 选择 Backend。
- Hatchet SDK、Client 和工作流注册延迟到实际使用；Worker 在 `main()` 中初始化。
- 保留工作流名称、版本、稳定幂等表达式、TTL、child step 与 durable polling 逻辑。
- `local` 不加载 Hatchet，不读取其 Token 文件；WINDEV-02 完成前返回未就绪。
  启用 Dispatcher 或 Reconciler 时先拒绝启动，不让占位实现消费 Outbox。
- 测试显式关闭自动 Dispatcher/Reconciler，已有执行 fixture 保持原样。
- Compose API/Worker 显式使用 `hatchet`，避免开发默认值改变现有集成路径。
- 配置错误和 SDK Token 验证错误不在常规异常文本中输出凭据。

## 数据库检查

原始 `.env.example` 的 API URL 为 `postgresql://localhost:5432/video-maker`。
这是可解析的 PostgreSQL URI，但缺少用户名和密码，并且 SQLAlchemy 默认驱动
与项目安装的 psycopg 3 不匹配。原 Better Auth URL 仍指向旧容器数据库。

已统一目标为 `localhost:5432/video-maker`，保留用户指定的 `progres`：

- API：`postgresql+psycopg://`，显式使用 psycopg 3。
- Better Auth：`postgresql://`，使用 node-postgres 接受的格式。
- 两个 URL 均包含凭据位置；公开样例仅保留口令占位符。

任务开始时不存在根目录或 `apps/api/.env`。已使用原有本机值生成被忽略的
根目录 `.env`，凭据经过 URL 编码；原始样例另存于被忽略的临时目录。
没有覆盖用户已有 `.env`，没有把实际口令加入提交。

验证分开记录：

1. URL 解析与 psycopg 3 驱动加载通过；不再从 `POSTGRES_*` 猜补连接参数。
2. Windows `postgresql-x64-16` 服务状态为 Running。
3. 使用修正后的完整 `.env` URL 只读连接时，IPv4 连接到服务器后仍收到
   用户 `progres` 的认证失败；IPv6 在当前执行环境被拒绝。
   因此数据库登录、库存在性与 Alembic head 尚未验收。

未执行迁移、建库、删库、清空数据、启动 WSL 或 Docker。用户名与密码的有效性
需由实际本机配置确认；不能把初始 URL 格式问题与后续认证结果混为一谈。

## 验证

在 `apps/api` 使用 Windows 原生 `uv` 执行，不要求 PostgreSQL 或 Hatchet Server。
当前环境的默认 uv 缓存不可写，命令显式使用项目内 `.uv-cache`。

```powershell
uv --cache-dir .uv-cache run --no-sync pytest -q tests/test_workflow_backend.py tests/test_hatchet_workflow.py tests/test_transactional_outbox.py tests/test_control_plane.py tests/test_hatchet_integration.py --durations=3
uv --cache-dir .uv-cache run --no-sync ruff check app tests
```

最终组合回归：**36 passed，1 skipped**，耗时 113.51 秒。API Ruff 静态检查
及 `git diff --check` 通过。跳过的是未显式启用的真实 Hatchet 集成测试；
本次未运行 Web/E2E 或 PostgreSQL 并发回归。

按用户要求，`AGENTS.md` 已记录任务验证完成后主动本地提交、报告提交号，
默认不推送的约定。

本次通过条件是无 Token 的 `local` 可导入 API、配置错误明确失败，以及现有
幂等和 Outbox 相关回归不退化；不是本地生成服务已经可以运行。

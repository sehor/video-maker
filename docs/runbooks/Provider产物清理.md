# Provider 产物登记与清理

BL-05 使用 `0015_provider_artifacts` 保存每个受控源对象/最终对象写入的 Job、Attempt、Project、
对象键、种类、保留到期时间与清理 Outbox 状态。先提交登记再调用 Provider 或写文件，因此
响应丢失、未产生结果、写入后宕机都保留可重试的清理记录。安全命名空间之外的对象不登记或删除。

`PROVIDER_ARTIFACT_RETENTION_SECONDS` 默认 86400（24 小时）。源对象至少保留到上传声明过期
及 Job 终态后的保留窗口结束；活动 Job 的对象不清理。已被 GenerationOutput 引用的最终对象
继续保留，直到项目删除。登记在写入之前，因此登记对象缺失本身不是失败，删除按幂等处理。

现有 Storage Cleanup 调度入口同时驱动产物 Outbox。清理采用 30 秒租约、失败退避及默认
5 次尝试；删除后宕机由过期租约重领。`PUBLISHED` 及 `cleaned_at` 表示删除已确认；
`DEAD_LETTER` 和 `last_error` 保留人工处理证据，排障后可经受控运维事务重置为 PENDING，
必须保留原对象身份并记录运维审计，不改写对象键。

项目删除事件包含全部已登记对象，每项保存 `not_before`；保留期未到时事件保持 PENDING，
不伪报 PUBLISHED，也不消耗失败重试预算。迟到的受控对象会加入/重新打开项目清单。
输入素材仍由项目删除的活动 Job 检查保护，镜头解绑不会删除素材或 Job 快照。

核查查询：

```sql
SELECT project_id, job_id, attempt_id, object_key, kind, retain_until,
       status, attempt_count, cleaned_at, last_error
FROM provider_artifacts ORDER BY project_id, created_at;
SELECT event_id, object_key, object_kind, not_before, status, cleaned_at
FROM storage_cleanup_objects ORDER BY event_id, object_key;
```

旧版本没有源对象登记。LocalStorage 升级后先运行只读盘点：

```powershell
uv run --project apps/api --no-sync python scripts/inventory_provider_artifacts.py
```

显式加 `--register` 仅把能由确切 Job/Attempt 命名空间验证归属的旧源对象加入 Outbox；
未知 UUID、异常布局或路径保留为 REVIEW_REQUIRED，不删除、不猜测归属。脚本不迁移数据库。
远程存储历史盘点需要适配器提供受控清单；当前不声称真实云对象盘点已完成。
开发库及已有对象本轮未迁移/盘点，以上为发布步骤；自动化只操作独立测试 schema 和测试目录。

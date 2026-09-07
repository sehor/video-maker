# BL-02A：输入快照契约与兼容迁移

日期：2026-09-07。按用户要求仅本地修复、验证和提交，不发布 GitHub Issue。

## 范围

新增 Job 输入快照类型、数据库列、不可变与格式约束，以及默认只检查的启用脚本。
快照结构见 [核心契约](../90_核心数据模型与接口契约.md)。
不接入单任务、Batch、Provider、重试或 UI；这些属于 BL-02B 及后续任务。
不迁移开发库，不启用真实路线，不修改账本或历史任务状态。

## 迁移与历史数据

- `0012_optional_media_hashes` → `0013_job_input_snapshot` 只新增可空列与触发器。
- 所有旧记录保持 SQL NULL，不读取当前 Shot、ShotReference 或素材来回填历史意图。
  因而不提供回填脚本；手工把 NULL 改成推测快照也会被不可变触发器拒绝。
- 新代码可在兼容阶段写入版本 1 快照；旧接单代码暂时仍可写 NULL。迁移本身不启用强制接单规则。
- 空库升级沿用既有迁移链；新增迁移不复用早期阶段二的破坏式替换方案。

## BL-02B 部署后的切换

以下步骤仅供接入完成后执行，BL-02A 不执行开发库切换。

1. 按原生维护手册预检并备份，先应用兼容迁移。
2. 部署并验证 BL-02B 的提交/执行接入，再暂停所有新接单入口和旧版本接单进程。
3. 等待未记录快照的旧活动 Job 自然完成；执行只读检查：

   ```powershell
   uv run --project apps/api --no-sync python scripts/job_input_snapshot_cutover.py
   ```

   输出仅包含必填状态、历史终态缺失数量，以及活动任务 ID、Project、Shot 和状态。
   `active_legacy_jobs` 即人工处置清单。无法排空时保持暂停并逐项调查，不自动回填输入、取消、
   修改状态或调整余额。活动任务检测查询为：

   ```sql
   SELECT id, project_id, shot_id, status FROM generation_jobs
   WHERE input_snapshot_json IS NULL
     AND status NOT IN ('SUCCEEDED','FAILED_FINAL','CANCELLED','EXPIRED','REJECTED_POLICY')
   ORDER BY id;
   ```

4. 清单为空、BL-02B 已通过验收时，显式执行：

   ```powershell
   uv run --project apps/api --no-sync python scripts/job_input_snapshot_cutover.py --enable
   ```

   脚本在一个事务中先获取 `generation_jobs` 的 ACCESS EXCLUSIVE 表锁，再检查旧活动任务，
   最后将 `generation_job_input_required` 启用为 ALWAYS。表锁覆盖检查和切换，等待之前未提交的
   接单事务，并阻止新插入穿过检查间隙。锁等待上限 5 秒，失败后整个事务回滚。
   清单非空时不更改规则并返回非零退出码；重复启用幂等。

5. 用已接入快照的版本恢复接单。新 Job（包括直接插入终态的记录）不能缺失快照；
   历史 NULL 终态仍可读取和更新非输入信息，但不能重新变成活动任务。

脚本只连接 `.env`/环境变量指定的现有原生数据库；不创建数据库、不启动服务。
不以 `required=true` 代替 BL-02B 功能验收，迁移 head 与必填触发器状态是两个独立事实。

## 回滚限制

- 尚未保存任何快照、且必填规则未启用时，可降级到 `0012_optional_media_hashes`；旧 Job 保留。
- 已有任意非空快照或已启用必填规则时，降级会拒绝；检查期间持有表锁，避免并发写入穿过。
- 不提供自动丢弃快照、关闭约束或恢复旧接单版本的路径。启用后回滚应用必须保留快照读写能力，
  否则保持暂停并制定单独的迁移方案。回滚不触碰账本。

## 验收

定向测试覆盖类型约束、空库升级/降级、带终态和活动任务的升级、SQL NULL 保留、数据库格式及
不可变保护、切换事务回滚、启用后必填、回滚保护，以及两个连接的接单/切换锁竞争。
数据库验证仅使用既有 Windows PostgreSQL 的独立测试 schema。

2026-09-07 实测：27 passed（38.06s，含 22 条纯逻辑与 5 条数据库迁移/约束测试）。
命令：`uv --cache-dir .tmp-uv-cache run --project apps/api --no-sync python scripts/test.py python apps/api/tests/test_input_snapshot.py apps/api/tests/test_input_snapshot_migration.py --run-db`。
补充 ORM 旧构造器省略快照/显式 None 的兼容断言后，定向复测 1 passed、4 deselected（8.81s）。
相关 Python Ruff check、格式检查和 Git 差异检查通过。
开发库保持 `0012_optional_media_hashes`，未执行开发库迁移或启用切换。

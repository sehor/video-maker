# TEST-01：降低数据库测试隔离成本

日期：2026-09-08。范围：共享测试 fixture、测试隔离分类与分阶段计时。
用户授权改进测试，减少重复清表；不修改账本规则、迁移或实例配置。
完整回归暴露的 Windows 路径表示问题做了必要的小修正，见下方记录。

## 实现

- 项目权限、PATCH、单连接模型约束共 15 项显式使用 db_rollback；应用 commit 释放
  保存点，整个用例结束回滚。SessionLocal 对象不替换，既有 import 使用同一工厂。
- 其余 database 用例保守地保留真实提交，包括并发、恢复、上传下载释放连接和延迟账本约束。
- 真实提交测试后，普通表按外键顺序 DELETE；仅当存在记录时，对不可删除的 ledger_postings
  和含循环外键的 generation_jobs 使用 TRUNCATE CASCADE。保持所有触发器启用，
  不重置序列，不再每次重建约 26 张表的文件；新 schema 和回滚用例之后不用重置。
- 迁移测试只使用自身 schema，不再为无关业务 fixture 做一次迁移和全表清理。
- 每次运行产出 python-phases.json，分开 setup/call/teardown、重置耗时和根表。

## 验收

新增 PostgreSQL 回归验证：保存点内 commit 可见但外部不可见、异常退出后恢复工厂和清除数据、
默认模式保留独立连接与真实提交可见性、普通清理保留表文件及路线种子、清理后账本不可删除
触发器仍生效。原业务断言未放宽。

初轮 20 passed、1 failed：新增连接可见性测试在 commit 释放连接前记录 PID，连接池随后
重新分配导致断言取错对象。改为在重新占用连接后读取 writer PID，保持独立连接断言。
最终定向回归 34 passed（19.84s），包括并发防超扣、账本与 Outbox 恢复；
`.test-runs/python-wb9bibjj/python.xml`。其中 setup 15.16s、业务执行 3.77s、teardown 0.27s，
数据重置 9.93s（包含在 setup 内），15 项回滚、19 项真实提交。

首次完整 Python：441 passed、1 failed、1 deselected，197.16s；
`.test-runs/python-cbks72e8/python.xml`。无数据库清理超时；setup 86.64s、call 103.85s、
teardown 2.19s，重置数据占 setup 中的 78.22s。15 项回滚、11 项独立迁移、130 项真实提交，
其余 286 项无业务数据库 fixture。发生 92 次限定根表的 TRUNCATE，未恢复全表 TRUNCATE。

失败发生于 poll/webhook 并发产物发布。Windows 的 Path.resolve 在父目录并发出现时，
可能保留 `\\?\` 扩展前缀；解析结果与不带前缀的存储根目录比较时被误判为越界。
本机 Python ntpath.realpath 会在先后两次系统调用的错误码相同（例如都是路径不存在）时
剥掉前缀；目录并发创建会使错误从路径不存在变成文件不存在，留下不同表示。
新增确定性注入复现：合法路径失败、越界路径拒绝通过；随后仅规范化**比较用**路径前缀，
实际 IO 仍用原解析路径，目录边界检查不删除。存储契约、webhook 与远端产物 56 passed
（21.20s），`.test-runs/python-w0tz8g8p/python.xml`；复现失败保留在
`.test-runs/python-irel1zw3/python.xml`。

最终完整 Python **444 passed、1 deselected，187.44s（3 分 7 秒），单次全绿**：
`.test-runs/python-52we0ool/python.xml`；命令
`pnpm test:python --run-db -o junit_family=xunit1 --durations=10`。

| 最终阶段 | 秒 |
|---|---:|
| setup | 87.61 |
| call | 94.60 |
| teardown | 1.70 |
| 数据重置（已包含在 setup） | 79.78 |
| 单次数据重置最大值 | 1.85 |

仍有 92 次限定根表的 TRUNCATE，这是保留真实提交、不可删除账本及循环外键的必要部分，
不是所有测试都回滚。15 项回滚、11 项独立迁移、130 项真实提交、288 项无业务数据库 fixture。
与此前 1640.65s 且包含 6 次清理超时的运行相比，实际总耗时降低约 88.6%；两次运行的
系统负载不受控，不能把该比例解释为排除环境因素后的纯算法加速。

相关 Ruff 与 Git 差异检查通过。未运行无关 Web/E2E、真实云服务或远程 CI。
本任务未发布远程 Issue，不推送；本地提交由本文件的 Git 历史定位。

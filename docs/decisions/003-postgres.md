# ADR 003：关系数据库发布与冻结迁移

状态：已实现并验证。记录日期：2026-09-12。

源记录版本、判断事件、企业成员和 lineage 是关联明确的持久状态。当前版本指针的切换需要与版本状态更新处于同一数据库事务，因此采用 PostgreSQL 关系表；无需引入图数据库。SQLite 用于本地无服务测试，不能替代 PostgreSQL 的真实事务验收。

`migrations/versions/0001_initial.py` 固定首次发布的 12 张表、外键、唯一约束和成员查询索引。迁移使用明确的 `op.create_table` 和逆序 `op.drop_table`，不导入应用 metadata，也不调用 `create_all`。后续模型变化必须新增迁移；修改应用 schema 不会改变已经发布的 0001。Alembic 的 env 只使用当前 metadata 做差异比较和生成后续草稿。

Alembic 支持 `ENTITYBRIDGE_DATABASE_URL`；默认配置是本地 `artifacts/entitybridge.db`。新数据库运行 `python -m alembic upgrade head`。现有通过 `Store.initialize()` 创建的数据库需要先核对实际结构，不能直接重新执行首次建表或不经核对就 stamp。当前 Store 的初始化仍负责建立默认 workspace 行；迁移负责结构，尚未替换该运行入口。

身份产物先写 `.partial`，flush/fsync 后 replace 为完整 JSON；随后登记候选版本，发布时再检查父版本、源输入和判断截止序号，在事务内切换当前指针。这个顺序允许留下未被数据库引用的完整文件，但不会把未完成文件发布出去。

失败恢复验收使用真实子进程。子进程执行实际 `Path.replace` 后发出边界标记并等待，父进程调用 `kill()` 强制终止，再创建新 Store 实例查询和重跑。测试钩子只存在于测试子进程，不修改服务实现。覆盖的是进程终止，不等同于断电、磁盘损坏或整个 PostgreSQL 服务崩溃。

实际验证：

| 命令／证据 | 本次结果 |
| --- | --- |
| `.venv/Scripts/python -m pytest tests/test_recovery.py -q` | SQLite 两项通过；未启用 PostgreSQL 时两项明确跳过 |
| 设置 `ENTITYBRIDGE_RECOVERY_POSTGRES=1` 后运行同一测试 | SQLite + PostgreSQL 共四项通过，2.49 秒 |
| 初始迁移往返 | 两引擎都完成 upgrade → metadata 零差异 → downgrade → upgrade → 零差异 |
| `scripts/check_failure_recovery.py` | SQLite：文件完整、未登记版本、旧版本可查、重跑发布、历史保留均为 true |
| `scripts/check_failure_recovery.py --postgres` | PostgreSQL 17.11：同一恢复验收全部为 true |
| `artifacts/reports/failure_recovery_sqlite.json` | 保存实际版本 ID、孤立文件字节数及 SHA-256 |
| `artifacts/reports/failure_recovery_postgres.json` | 保存 PostgreSQL 独立运行的相同证据 |

PostgreSQL 验证只从忽略提交的 `.tools/database.json` 读取连接参数，并仅在 `entitybridge_test` 中新建随机 `recovery_*` schema，结束后删除该 schema。首轮曾因本地 PostgreSQL 已停止而连接超时；启动已有项目内 PostgreSQL 17.11 后重新验证成功，未安装系统服务。测试连接错误使用脱敏摘要，不输出密码。

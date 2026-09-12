# ADR 009：可恢复的持久匹配任务与事务内结果绑定

状态：已实现；SQLite 和 PostgreSQL 的租约、进程中断、取消、回滚与迁移回归通过。

## 问题与目标

同步匹配占用 HTTP 请求的生命周期。客户端超时后无法确定匹配是否完成，重复点击可能再次计算；工作进程被终止后也没有可恢复的任务状态。实体版本本身已有准备与发布的边界，本次把执行状态补进这一边界，保留显式发布。

引入 `entitybridge.jobs.JobQueue`，继续使用应用已有的 SQLite 或 PostgreSQL。API/CLI 提交任务，独立 worker 领取并计算，成功只产生 prepared revision。任务列表可以查询进度、失败代码、尝试次数及结果版本；任务成功不会移动已发布的 current 指针。

## 状态和数据契约

`durable_job` 保存请求 JSON、请求摘要、幂等键、入队时的来源摘要/已发布父版本/判断事件截止序号、状态、尝试预算、租约、进度、失败分类和结果版本。`durable_job_event` 追加提交、领取、租约过期、取消、失败、重试和成功事件。状态为：

| 起点 | 条件 | 终点 |
| --- | --- | --- |
| queued | worker 领取且输入仍有效 | running；attempt 加一，生成新 token |
| queued | 取消 | cancelled；立即完成取消 |
| queued 或过期 running | 输入已变化 | failed；`stale_basis` |
| running | 租约过期且预算剩余 | running；新 owner/token，attempt 加一 |
| running | 租约过期且预算耗尽 | failed；`attempts_exhausted` |
| running | 计算异常 | failed；保存安全分类，不保存异常原文 |
| running | 收到取消请求，worker 下一次检查或重领时观察到 | cancelled |
| running | 准备版本与任务完成在同一事务提交 | succeeded |
| failed | 显式重试，预算剩余且原输入仍有效 | queued |

`max_attempts` 范围为 1 到 20。普通执行失败不会无限自动重试；用户显式重试。死进程的过期租约可以被后续 worker 自动重领，依然消耗同一预算。取消是终态，不能通过 retry 撤销；重新执行需要新的提交和幂等键。

同一幂等键与同一请求 JSON/预算返回原任务。重复键对应不同请求或预算时拒绝。即使原任务已经完成，重试提交仍返回原结果；这也意味着重新基于新数据运行需要新幂等键。输入依据由服务器在工作区写锁内读取，客户端不能指定或替换它。请求模型只允许匹配设置；worker 另外绑定模型和流水线指纹。

公开查询不返回 `lease_token`。失败数据只含预设 `code` 和安全 `message`；数据库异常可能携带连接串、SQL 或来源数据，因此不保存 `str(exception)`。

## 租约与并发

所有队列状态修改先获取已有的工作区写锁，与导入、判断和发布采用相同的锁顺序。领取在数据库事务中完成，不依赖进程内互斥锁。每次领取都生成新 UUID token；更新要求同时满足 job ID、owner、token、attempt、running 状态和未过期租约。旧 worker 即使恢复执行，也不能续租、写失败状态或绑定结果。

租约使用数据库时钟。PostgreSQL 的 `clock_timestamp()` 返回实际调用时间，而 `now()` 固定为事务开始时刻；等待锁后不能用过早的事务时刻续租。[PostgreSQL 时间函数文档](https://www.postgresql.org/docs/17/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)。SQLite 使用 `julianday('now')` 转为 Unix 秒。[SQLite 日期时间函数文档](https://www.sqlite.org/lang_datefunc.html)。代码在来源摘要扫描后重新取时间，再发放租约；提交前的输入扫描结束后再次检查期限。

`run_once` 启动后台心跳，默认每个租约时长的三分之一续租。回调可调用 `check_active` 实现更及时的协作取消。心跳不能强制打断正在执行的 Python 或原生库计算。最终事务内的检查保证忽略取消或续租失败的回调不能绕过提交规则。

租约应长于一次最长的数据库写事务和可能的调度暂停。准备版本最后写入成员/投影时持有工作区锁，另一个连接上的心跳会等待；若事务耗时超过剩余租约，完成钩子拒绝并回滚，保持正确性，但需要更充足的租约或减少事务工作来恢复可用性。这里没有对任意慢任务“必定成功”的保证。

## 原子完成与重复执行边界

`Store.prepare_revision` 接收内部钩子：读取依据时和最终写入事务开始时执行 `basis_guard(con)`，写完版本、成员、lineage、投影后执行 `commit_hook(con, revision_id)`。worker 使用：

```python
queue.run_once("worker-unique-id", lambda lease: store.prepare_revision(
    edges,
    basis_guard=lambda con: queue.validate_basis(con, lease),
    commit_hook=lambda con, revision_id: queue.bind_prepared(con, lease, revision_id),
))
```

`validate_basis` 在传入连接中锁住工作区，检查实时租约、取消标志，以及来源 hash、parent、event cutoff。`bind_prepared` 再检查这些条件，并核对候选自己的输入摘要、父版本、事件截止点及 prepared 状态，然后把任务改为 succeeded。**候选登记与任务完成使用同一个数据库事务。** 回调仅返回一个 revision ID、但没有调用原子绑定钩子时，任务失败为 `missing_atomic_binding`。

| 故障时点 | 持久结果 |
| --- | --- |
| 计算中进程死亡 | running 租约过期后可以重领 |
| 产物文件替换后、数据库提交前死亡 | 可能留下孤立文件；数据库没有该候选或成功状态，当前版本不变 |
| 完成钩子后、事务提交前异常 | 候选、成员、投影、成功事件和 succeeded 状态一起回滚 |
| 候选与成功状态提交后、worker 返回确认前死亡 | 后续 worker 读到 succeeded 和已绑定版本，不重新领取 |

计算交付语义是 **at least once**：CPU 计算及文件写入可能重复，不能宣称端到端 exactly once。已集成的准备版本提交路径对同一任务只保留一个事务绑定结果；幂等键和唯一结果关联避免把客户端重试变成新的可见身份版本。回调执行邮件、外部 HTTP 写入等额外副作用不在此事务中，本接口没有为它们提供 exactly once。发布仍要求独立操作，并继续检查当前数据、父版本与投影完整性。

## 升级与恢复

新增迁移 `0004`；已发布 `0001`、`0002`、`0003` 文件保持冻结。wheel 内的 `database.initialize_database` 可以创建空库，或核验已知 0001/0002/0003 的完整结构后依次安全升级；未知结构或结构与 stamp 不一致时拒绝修改。迁移只增加任务表和索引，不改来源事实、旧产物或已发布指针。

安装后的升级入口仍为 `entitybridge init-db`。源码检出环境也可在配置好目标数据库后使用 `python -m alembic upgrade head`。两条路径均有验证，不依赖 wheel 中存在仓库根目录的迁移脚本。

逻辑恢复流程在同一恢复事务调用 `cancel_restored_jobs(con)`：queued/running 变为 cancelled，清 owner/token/期限，追加 RESTORE_CANCEL。恢复后的成功任务及其 prepared 绑定保留；旧租约绝不能在新环境继续执行。此 helper 要求由已获得目标数据库独占控制的恢复流程调用。

## 实测与边界

`tests/test_jobs.py` 的 23 个行为用例覆盖：并发幂等提交与领取、过期替换后的旧 token 拒绝、尝试耗尽、三种依据变化、取消、提交阶段过期、事务整体回滚、长回调续租、原始异常脱敏、错误候选绑定拒绝、完成后迟到异常、恢复取消，以及 0001/0002/0003 升级保留来源记录。

其中两条使用真实子进程和强制终止：一次终止发生在领取后，一次发生在候选与任务完成共同提交后。前者由新 worker 第二次尝试恢复，后者保持第一次 succeeded 且不会重领。SQLite 与真实 PostgreSQL 都执行这些行为；另外运行现有 recovery 的完整 Alembic 升降级和产物替换后 kill 演练。

这是共享同一数据库和产物目录的应用任务队列。工作区锁串行化状态修改，尚未实现多租户分片、优先级、公平调度、自动指数退避、专用 broker、远程产物复制或集群选主。大型队列的领取扫描和高写入竞争未做吞吐量承诺。SQLite 适合单机低并发；PostgreSQL 支持多个进程使用相同协议，但这些测试不构成生产分布式队列或可用性 SLA 认证。

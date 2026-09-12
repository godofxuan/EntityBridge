# ADR 011：一致逻辑备份、隔离恢复与 HTTP 容量探测

状态：已实现；SQLite 与 PostgreSQL 17.11 的合成恢复演练通过。当前能力适用于单工作空间的开发、验收和灾备演练，生产切换仍需独立验证。

## 备份边界与一致性

`operations.create_backup(store, destination)` 在一个数据库读快照中导出当前版本的全部 EntityBridge 表，包括来源记录及历史版本、决策事件、发布指针、历史与 prepared revision、查询投影、任务及任务事件。它不是只拷贝最新 JSON。

SQLite 先检查源文件存在，再以 `mode=ro` 打开并显式开启读事务，源路径缺失或检查后被删除都不会新建空库；PostgreSQL 使用 `REPEATABLE READ` 只读事务，使连续读取落在同一个数据库快照。普通业务写入可以继续，但备份期间须停止 schema 迁移、产物删除和外部改写。SQLite 默认回滚日志模式下读事务可能延迟写入；WAL 下可以与写入并行。回归测试在复制期间提交一次来源更新，验证备份没有混入后续状态。[SQLite 隔离说明](https://www.sqlite.org/isolation.html) · [PostgreSQL Repeatable Read](https://www.postgresql.org/docs/17/transaction-iso.html#XACT-REPEATABLE-READ)

每个已注册 revision 的不可变产物都必须存在，内容 SHA256 必须等于数据库绑定值。备份还检查来源版本引用、历史父链和完整投影；缺失投影时失败，不在备份过程中修改源库。可先显式运行投影核验/重建，再重新备份。

备份目录包含 `database.json`、`artifacts/<revision>.json` 和最后写入的 `manifest.json`。清单记录 schema 版本、逻辑数据库及各产物的字节数/SHA256、每表行数/摘要、发布指针和历史查询摘要。文件写入会执行 fsync；缺少最终清单、文件缺失、额外成员、路径穿越、符号链接或摘要不符都会拒绝验证和恢复。

SHA256 用于完整性核验，不提供签名、加密或备份来源认证。备份含实际来源内容、审核人员与审核理由，应存入受控且加密的存储，不能上传到公开仓库。模型文件、外部原始下载、令牌配置、数据库角色、操作系统配置不属于这个备份；需要另行保留对应版本和访问控制。

这是应用级逻辑快照，不包含 WAL、物理页或系统角色，也不支持任意时间点恢复。要求更小数据损失窗口时，应另行设计 PostgreSQL 基础备份、WAL 归档、密钥托管与恢复演练；不能把本功能称作已完成 PITR。[PostgreSQL 连续归档与 PITR](https://www.postgresql.org/docs/17/continuous-archiving.html)

## 恢复保护与任务处置

`restore_backup(backup_directory, target_database_url, target_artifact_root)` 会先把备份完整读入并验证，再创建目标：

- SQLite 仅允许新持久化文件，用独占创建预留文件；已存在的空文件也拒绝。
- PostgreSQL 仅允许名称以 `_test` 结尾的数据库，函数内部创建新的随机 `restore_<uuid>` schema，不接收现有 schema 名。返回值包含该 schema 名，不包含连接密码。
- 产物目录必须不存在，且不能位于备份目录内；拒绝通过连接参数覆盖数据库或 schema。

`artifact_path` 被规范化为新产物目录下的文件名。历史数据库中的绝对路径只有仍位于源产物根目录内时才能备份，恢复后不会继续读取源文件。新目标按记录的当前 schema 创建，导入全部行并重置 PostgreSQL 序列，再核验来源版本、决策历史、发布指针、产物摘要、完整投影和全部已发布实体查询。与备份 schema 不兼容的程序版本会拒绝恢复。

恢复不复用旧 worker 的租约：所有 queued/running 任务在恢复事务中变为 cancelled，清空 owner/token/lease_until，并追加 `RESTORE_CANCEL` 事件。原始输入和既有事件保留；已完成任务的 prepared revision 绑定保留。需要重新计算时，审核输入和当前来源状态后显式提交新任务，不能把恢复后的旧队列直接接回 worker。

受控异常会回滚并删除本次创建的目标；源库、源产物和已有目录不受影响。进程或机器在恢复中途崩溃可能留下未完成的新目标：它不会被自动当作有效恢复，下一次恢复应使用另一个新目标，隔离检查后再处理残留。当前 PostgreSQL 恢复仅支持 `_test` 隔离演练，没有实现直接覆盖业务库、生产重命名切换或自动故障转移。

## 操作入口

先通过受控环境设置源 `DATABASE_URL`；恢复目标另用 `ENTITYBRIDGE_RESTORE_DATABASE_URL`，不要把真实密码写进命令历史或报告。确认 `--artifact-root` 对应这个源数据库。

```sh
entitybridge backup --artifact-root artifacts/revisions --output artifacts/backups/backup-001
entitybridge verify-backup --input artifacts/backups/backup-001
entitybridge restore-backup --input artifacts/backups/backup-001 --output artifacts/restored/revisions-001
```

库接口返回的恢复检查必须全部通过，之后再用恢复目标的连接和新产物目录检查历史页面、别名搜索、具体实体详情。PostgreSQL 返回的随机 schema 需要显式配置在新连接的 search_path 中。保持旧环境不变，待演练验收后单独制定切换步骤。

复现合成演练：

```sh
python scripts/check_backup_restore.py --records 1000 --output artifacts/reports/my_backup_sqlite.json
python scripts/check_backup_restore.py --postgres --records 1000 --output artifacts/reports/my_backup_pg.json
```

PostgreSQL 脚本要求通过 `ENTITYBRIDGE_TEST_DATABASE_URL` 提供无 schema 覆盖的 `_test` 数据库连接，并自动创建/清理随机源与恢复 schema。`--local-postgres` 是显式使用本机私有配置的便利入口；公开报告不包含配置内容。

## 恢复演练实测

2026-09-12，同一台 Windows 11、12 逻辑 CPU、约 31.6 GiB 内存的机器上，分别建立 1,000 条合成记录、初始 500 个实体、4 个已发布或 prepared 版本。包含来源变更、人工决策和 queued/running/succeeded 任务快照；数据库逻辑数据约 4.13 MB，产物约 3.68 MB。

| 后端 | 备份秒 | 独立核验秒 | 恢复并核验秒 | 结果 |
|---|---:|---:|---:|---|
| SQLite | 0.597 | 0.383 | 0.965 | 12 项检查通过 |
| PostgreSQL 17.11 | 1.087 | 0.606 | 3.494 | 12 项检查通过 |

两个后端都取消了 2 个未完成任务，保留成功任务的 prepared 绑定，并验证后续事件自增插入正常；PostgreSQL 随机 schema 已清理。数据为合成样本，运行时 UUID 和时间戳会变化，复现不要求生成相同文件哈希。数值报告分别为 `backup_restore_sqlite_v04_release.json` 与 `backup_restore_postgresql_v04_release.json`（最终 schema 0005）。

这些耗时没有包括远端下载、数据库供应、密钥恢复、业务核验或流量切换，不能当作生产 RTO。当前没有自动备份调度或异地复制，也没有已承诺的 RPO。应由业务确定频率和保留期，对每次备份执行完整核验，并周期性恢复到独立目标；不能仅检查“备份命令退出成功”。目前逻辑导出和验证会在内存中保留全部表及产物，长历史和大库须另测内存与耗时，再决定流式实现或数据库原生备份方案。

## 实际 HTTP 负载实验

`check_service_load.py` 启动独立 Uvicorn 子进程，绑定 loopback 临时端口，使用随机 viewer bearer token。先验证无令牌请求返回 401，再验证有权限请求命中本次合成数据的固定 revision。客户端通过真实 HTTP/1.1 持久连接发请求，没有使用 TestClient 或直接调用 Store 代替网络。

实验固定 10,000 条合成记录、5,000 个实体、1 个 API worker、8 个并发客户端、15 秒发请求窗口。40 次预热后，按轮转顺序混合第一页、深页、别名包含和无匹配四种查询；每次校验状态、revision、总数和响应行数。所有延迟从发出 HTTP 请求测到响应体接收完毕，包含认证、API、数据库、序列化和 loopback 开销。请求在窗口结束前发出后仍会等待完成，因此实际统计时间略长于 15 秒。

首个有权限请求单列延迟，但操作系统缓存已被合成数据准备过程预热，不能称作冷磁盘。进程资源由 worker 回报真实 PID，并校验它属于本次启动的进程树；每 50 ms 采样 RSS，CPU 时间仅覆盖负载窗口。

| 后端 | 完成请求 | P50 ms | P95 ms | 请求/秒 | 错误率 | API CPU 秒 | API 峰值采样 RSS MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| SQLite | 2074 | 53.66 | 105.57 | 137.18 | 0.0% | 43.09 | 183.98 |
| PostgreSQL 17.11 | 1699 | 53.26 | 190.80 | 112.23 | 0.0% | 15.14 | 168.54 |

首个有权限请求分别为 SQLite 12.11 ms、PostgreSQL 107.45 ms。API CPU 秒是各线程累计时间，可以大于墙钟时间；这些 CPU/RSS 数字不包含 PostgreSQL 数据库进程或负载客户端，不能直接当作两种后端的系统总资源比较。两次测量的所有响应都通过内容范围核验，PostgreSQL 临时 schema 和本次服务进程均已清理。

复现命令：

```sh
python scripts/check_service_load.py --records 10000 --concurrency 8 --seconds 15 --output artifacts/reports/my_load_sqlite.json
python scripts/check_service_load.py --postgres --records 10000 --concurrency 8 --seconds 15 --output artifacts/reports/my_load_pg.json
```

每个报告记录配置、软件版本、硬件、实际执行路径相关的模块摘要以及运行期间代码是否变化。最初试跑的 HTTP 响应有效，但资源采样错误地读取了 Windows Python launcher，故早期 `service_load_*_v04.json`/`*_final.json` 的 CPU/RSS 不能引用；修正后的 `*_verified.json` 才用于这里的资源结果。旧报告保留，不覆盖原始观测。

这是一个固定负载点，客户端与服务端共享机器，也没有控制后台任务和缓存环境，不能用两行数据证明某个数据库普遍更快。它没有覆盖写入竞争、同时匹配任务、TLS、代理、SSO、WAN 或长时间故障。闭环客户端会在请求慢下来时减少发请求速率，不能据此推断开放流量下不会积压；结果不是最大容量或生产 SLA。

## 观察与故障处理

`/health` 只用于进程存活；管理员 `/ops/status` 检查数据库连接与当前投影就绪，不等同于完整数据审计。管理员 `/ops/metrics` 是单 API 进程自启动以来的计数和有限近期延迟样本，重启会清空，不是集中监控、持久时序库或全链路追踪。

| 信号 | 初始处置建议 | 已有操作 |
|---|---|---|
| 数据库 readiness 失败、查询 5xx | 停止提交新计算，先排查连接、磁盘和数据库状态；不直接重试发布 | `/ops/status` 与受控服务日志 |
| 同类流量 P95 持续超过本机基线两倍 | 对照请求量、CPU、RSS、数据库等待和后台 worker；这是排查门槛，不是业务 SLO | `/ops/metrics`，重新运行固定条件负载探测 |
| queued 持续增长或 running 租约到期 | 检查 worker 是否运行及其模型文件；按队列的租约、重试和幂等规则处理 | job 查询、取消/重试命令，见[任务队列 ADR](009-durable-jobs.md) |
| 投影缺失或完整核验失败 | 停止发布，先验证不可变产物哈希；产物正常时显式重建投影，再完整核验 | `verify-projection` / `rebuild-projection` |
| 备份缺失、校验失败或恢复演练失败 | 保留失败样本和源环境，换新目标调查；不能用有问题的备份覆盖当前库 | `verify-backup` 与独立恢复演练 |

这些是 runbook 建议，没有自动向外发送告警，也未部署采集器、值班路由或灾备控制面。上线前仍需根据真实流量定义告警窗口、错误预算、备份计划与责任人，并验证凭据轮换、网络边界和容量。现有单工作空间角色令牌不能冒充 OIDC/SSO、用户生命周期管理、跨租户隔离或完整企业认证体系。

最终操作验证、较早负载源码快照与证据口径见 [v0.4 验证记录](../evaluation/V04_VALIDATION.md)。工作区模式备份在同一只读数据库快照中检查既有归属，不通过初始化/迁移来获取备份权限。

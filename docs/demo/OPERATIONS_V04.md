# v0.4 操作入口

先按 README 安装，再用受控环境提供数据库连接和角色令牌。下列命令不需要把真实密码写进文件或命令行；具体模型目录必须来自自己的冻结训练。

## 提交和执行任务

普通同步 `POST /match-runs` 保持兼容。管理员在网页 `/tasks` 或 `POST /jobs` 提交后台任务：

```json
{"settings":{"method":"exact","threshold":0.9,"review_threshold":0.5},"idempotency_key":"my-import-20260912","max_attempts":3}
```

HTTP 202 表示已持久接收。另一个终端运行 `entitybridge worker`，或用 `entitybridge worker --once` 处理最多一个任务后退出；API 和 worker 必须使用同一数据库、产物目录、程序版本与模型。使用 Splink 时两端都传相同的 `--model`，hybrid 还需相同 `--candidate-model`。不要在活跃进程中就地替换安装源码；发布程序更新时重启进程，旧任务会检查冻结指纹是否过期。

GET `/jobs/{job_id}` 可查看状态与事件，POST `/jobs/{job_id}/cancel` 取消，POST `/jobs/{job_id}/retry` 显式重试。API 最多允许 5 次尝试；队列库最多 20 次。相同幂等键只对应一个请求，想基于更新后的来源再运行应使用新键。

`succeeded` 只表示存在可检查的候选版本。管理员检查历史页面，再通过发布表单或 POST `/revisions/{revision_id}/publish` 提交期望父版本。任务完成与正式可见是两个不同状态。

## 复核数据与候选模型

reviewer/admin 可调用 GET `/learning/summary` 查看有效人工标签数量，GET `/reviews/queue?strategy=uncertainty_diversity&limit=50` 获取建议排序，`strategy=random` 获取固定种子的对照排序。已明确二元判断及当前 suppressed 的配对被排除，包括暂缓和撤销形成的抑制；自动接纳/低分项仍可用于抽检。分数接近 0.5 只是排序启发式，不保证信息增益。

```sh
entitybridge export-labels --output artifacts/learning/reviews-001
entitybridge train-review-model --input artifacts/learning/reviews-001 --output artifacts/learning/candidate-001
```

加 `--calibrate` 时需要独立且足量的校准切分。未发布来源/判断、标签矛盾、跨切分共享依赖或样本不足都会明确拒绝。候选模型只产生冻结模型和评估报告；没有自动部署操作。后续撤销不会改写已导出的历史快照，任何未来部署都须重新检查判断依据。

## 运行检查与恢复

管理员 GET `/ops/status` 查看数据库与当前投影就绪；GET `/ops/metrics` 查看当前 API 进程的请求计数和有限近期延迟。指标随进程重启清空，没有集中告警或分布式追踪。

```sh
entitybridge verify-projection
entitybridge rebuild-projection
entitybridge backup --output artifacts/backups/backup-001
entitybridge verify-backup --input artifacts/backups/backup-001
entitybridge restore-backup --input artifacts/backups/backup-001 --output artifacts/restored/revisions-001
```

恢复要求显式设置 `ENTITYBRIDGE_RESTORE_DATABASE_URL`。目标 SQLite 文件/产物目录必须全新；PostgreSQL 仅支持 `_test` 数据库中的新随机 schema 演练。逻辑备份不是 WAL/PITR，也没有自动生产切换。[完整边界](../decisions/011-operations.md)

## 身份与隔离部署

`ENTITYBRIDGE_OIDC` 配置可信 issuer/JWKS/audience/workspace 后，可以验证已经签发的 JWT。它不是浏览器 OAuth 登录流程；JWT cookie 每次请求重新验签/检查时效。[配置与 claim](../decisions/012-identity-boundary.md)

多个工作区使用独立数据库、目录和 API/worker 部署，通过 `--workspace-config` 与 `--workspace` 在进程启动时选择。[配置示例和限制](../decisions/013-workspace-isolation.md)

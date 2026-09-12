# v0.4 验证记录

日期：2026-09-12。项目版本 `0.4.0`，数据库 head `0005`。这是实际执行结果；配置了 CI 不等于远程运行通过，公开状态以 [Actions](https://github.com/godofxuan/EntityBridge/actions) 与对应发布提交为准。

## 交付范围

| 原差距 | 本轮实现与验收 | 尚未证明 |
|---|---|---|
| 长任务依赖 HTTP 请求 | 数据库持久任务、幂等、取消、租约、有限重试，候选与成功原子绑定；真实进程 kill/重领测试 | broker 规模、公平调度、高写入吞吐、集群 SLA |
| 复核没有训练出口 | 有效显式标签导出，版本/撤销约束、依赖组件切分、候选训练、独立可选校准、主动/随机采样比较 | 真实人时收益、无偏业务标签、自动部署 |
| 运维缺少实测 | 两后端逻辑备份/恢复、真实 Uvicorn HTTP 固定负载、管理指标与就绪接口 | WAL/PITR、生产故障切换、集中告警、长稳和最大容量 |
| 身份仅本地令牌 | 固定 RS256 JWT 验签、可信 issuer/JWKS/audience/工作区角色、密钥轮换与超时测试 | 真实企业 IdP 接通、浏览器 SSO、实时撤销 |
| 工作区误配 | 独立数据库/目录/进程，配置重复检查与数据库持久归属绑定，备份读快照内校验归属 | 共享表多租户、自助管理门户、独立主机部署认证 |
| 更难的外部证据不足 | WDC 未见商品实体首次评分；公开修正验证重叠，模型/阈值先冻结，原测试保持完整 | 企业场景时间外泛化、优于成熟产品 |

操作命令见 [v0.4 操作入口](../demo/OPERATIONS_V04.md)，设计取舍见 ADR 009–013。

## 最终测试与安装包

本机 Windows / Python 3.12.13 完整运行 **298 passed，0 failed，0 skipped**，耗时 110.06 秒，启用实际 PostgreSQL 测试连接。涉及 Store、任务、工作区、备份与恢复的相关 fixture 使用真实 PostgreSQL；不是说全部 298 项都运行在 PostgreSQL。Ruff、依赖一致性和包内资源检查通过。

首轮完整测试暴露一个重构后的测试目标错误：特征版本常量已迁到 `matching_service`，旧测试仍向 `api` 注入同名变量。改为检查实际使用位置并去掉静默添加属性后，特征变化全量重算回归通过。另在本轮审查中修复了暂缓配对重入主动队列、PG 端点参数覆盖、重复工作区令牌和 CLI 备份绕过工作区归属的问题，均有回归证据。

保留两条依赖弃用警告：Starlette 的 httpx TestClient 路径和 AnyIO BlockingPortal 导入别名。当前验证通过，没有把警告当作已修复。

最终 wheel：`entitybridge-0.4.0-py3-none-any.whl`，**97,454 字节**，SHA-256：

```text
c67371e3411c808b5558f9993fd7d9570fb51fc9988ac0c9ace26525f139292c
```

31 个应用文件与源文件逐字节一致。通过已发布 v0.3 wheel 创建数据库，在源码目录之外安装 v0.4 wheel 并升级到 `0005`，验证旧发布版本/实体不变、投影完整、HTML/CSS/API 可用，以及独立 CLI worker 成功但不自动发布。[安装包验证](evidence/operations_v04/wheel-v04-final-smoke.json) · [测试摘要](evidence/operations_v04/validation_summary.json)

## 真实数据与运维实测

真实 10,000 来源记录进入 API 后持久入队；独立 CLI worker 只执行一次，产生 13,033 个候选和 4,438 个结果实体。结果实体数不是准确率真值。候选在发布前不可通过企业目录/导出读取，显式发布后核验固定版本目录、详情、22,160 个规范字段来源和全部 10k 记录版本导出。**10 项检查通过**，schema 已清理，总流程 38.208 秒，其中 worker 16.197 秒。传输为进程内 ASGI API 加独立 worker 和真实 PostgreSQL，不能把此时长称为网络吞吐。[报告](evidence/operations_v04/durable_workflow_v04_release.json)

在最终 `0005` 上，两后端各恢复 1,000 条合成记录和 4 个版本，**各 12 项检查通过**。queued/running 共两个任务被取消，成功任务的候选绑定保留，后续事件序号正常增长；PG 源/恢复 schema 已清理。SQLite 恢复并验证 0.965 秒，PG 3.494 秒，不含环境供应与业务流量切换，不能作为生产 RTO。[SQLite](evidence/operations_v04/backup_restore_sqlite_v04_release.json) · [PostgreSQL](evidence/operations_v04/backup_restore_postgresql_v04_release.json)

真实网络负载使用已封存的较早实现快照：10k 合成记录 / 5k 实体、单 Uvicorn 进程、8 个并发客户端、15 秒固定只读混合查询，未覆盖新 OIDC 或并发匹配。SQLite 完成 2,074 请求，P95 105.57 ms；PG 完成 1,699 请求，P95 190.80 ms；两者零错误且逐响应核验内容。它是一个同机闭环负载点，不是最大容量、生产 SLA 或数据库优劣排名。早期错误采样到 Python launcher 的资源数字未使用；这里只引用实际 server PID 的 verified 报告。[负载设计](../decisions/011-operations.md) · [SQLite](evidence/operations_v04/service_load_sqlite_v04_verified.json) · [PostgreSQL](evidence/operations_v04/service_load_postgresql_v04_verified.json)

## 评测中的失败结果

主动学习模拟仅使用原 DBLP 训练区重分出的数据，三种种子、20/50/100/200 获取预算、另加固定 112 个验证标签。24 个检查点的测试 F1 都为 0.78448。主动方法获得更多正例，但未带来 F1 增益；不能称人工效率提升。[协议](../decisions/010-review-learning.md) · [报告](evidence/operations_v04/learning/report.json)

WDC 下载集预检发现训练/附加验证重叠。先固定验证修正，再冻结四方法对完整 4,500 对 test 首次评分；训练/保留验证/测试的记录和产品簇交集均为零。商品字段模型 F1 0.057803、Recall 0.0300；10 FP + FN 成本为 525，高于标题模型的 492。测试仅一个依赖组件，因此区间不可估计，没有虚构独立样本。保留公开样例已被查看和修正协议的事实，不声称与原论文同协议或从未见过任何测试例子。[详细结果](WDC_UNSEEN_RESULTS.md) · [冻结报告](evidence/operations_v04/wdc/report.json)

## 公开证据边界

[本轮证据哈希](evidence/operations_v04/SHA256.json)包含汇总、模型参数和实际执行源码归档。真实 10k/最终恢复三报告对应 44 文件运行前后摘要一致的源码包；负载和 WDC 各保留其独立快照。发布不覆盖旧报告，也不把不同阶段的性能值当作同一安装包测量。

原始数据、逐对分数/ID、学习获取轨迹、真实复核人员/原因、数据库、连接秘密、私有日志与根 Git 历史均不公开。新外部来源、真人复核收益和真实部署仍需独立证据，不能从合成演练推导。

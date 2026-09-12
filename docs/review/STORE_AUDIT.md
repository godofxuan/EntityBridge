# 持久化、身份与撤销审查

审查日期：2026-09-12。范围：Store、关系表、数据库初始化/迁移、身份 lineage、确定性 resolver、增量闭包以及公开查询的版本边界。数据集匹配质量、HTTP 权限和互联网部署由另外的审查覆盖。

## 已复现并修复

| 问题 | 修复前的可复现行为 | 修复与回归证据 |
| --- | --- | --- |
| 历史内容去重错误阻止数据恢复 | 导入 A → 墓碑 → 再导入相同 A，返回旧导入结果，但当前记录仍不存在；名称 A → B → A 也无法恢复 A | 仅当旧导入结果的全部记录版本仍为当前版本时，才当作重试返回。再次观察到历史内容会保存新快照与新记录版本。`test_reimport_old_content_restores_tombstoned_record_without_reusing_its_old_version` 修复前失败于当前记录为空，修复后通过 |
| 撤销预览可能携带不属于其计算结果的截止序号 | 在预览读完 cutoff=1 的约束后，第二个真实事务提交新判断；原代码再次读取事件序号，返回旧的 4 簇预览却标为 cutoff=2 | 只捕获一次事件截止序号，计算约束与返回响应使用同一值。后续撤销会拒绝过时预览。`test_preview_uses_the_event_cutoff_that_actually_produced_its_partitions` 修复前实际得到 `2 != 1`，修复后通过 |
| 人工已处理的评分边仍显示待复核或依据不足 | 混合候选评分 1.0 经人工 accept/reject 并发布后，`/reviews?status=review` 仍显示同一记录对；普通低分边也会遮蔽有效人工判断 | 评分行优先展示直接 must/cannot 与仅由 must 形成的传递确认，保留原始分数；review-only 边遇簇级 cannot 展示冲突，绝不自动 union。新增七项解析回归与真实 HTTP 筛选回归均经历失败→修复→通过 |

快照内容由唯一约束改为普通索引属于必要的数据模型修正：内容相同不代表同一次观察。首次发布的 `0001` 迁移未改动；新增 `0002`。降级若遇到重复观察会明确失败，不删除或合并历史来迎合旧唯一约束。

## 新增的业务契约

混合候选召回尚不足以支持自动合并，因此评分边可携带 `auto_merge=False`。resolver 保留原始分数，状态为 `review`，原因是 `candidate_requires_review`，不创建自动支持边；显式人工 must-link 仍可合并，撤销仍遵守抑制与替代路径语义。完整版本 JSON 和数据库评分证据均保留该标志。

`Store.prepare_revision` 增加可选 `force_full` 与 `force_full_reason`，使全局候选算法能明确要求全量解析，同时保持策略指纹稳定。默认接口兼容旧调用。增量比较也将 `auto_merge` 纳入边变化检测，避免只改策略标志而漏掉重算。

评分解释现明确区分人工确认与自动连通：直接 accept 使用 `explicit_must_link`，直接 reject 使用 `explicit_cannot_link`，纯人工支持路径使用 `transitive_must_link`。仅因其他自动边已经连接，不会把一个尚未复核的 review-only 候选标成人工确认。新的解析器版本需要进入调用方策略指纹，触发新运行重算；已有冻结产物不改写。

新增回归从真实 Store 接口验证：评分 1.0 的 review-only 边仍分为两个实体 → 人工接受后合并 → 撤销后拆开。另验证同一策略下改变自动合并标志时，增量结果等于全量结果。这里是治理行为测试，不是精度收益。

## 已检查的边界

- 多次确认同一记录对是独立证据。正序或逆序撤销其中一个，不取消其余有效证据；全部撤销后不会重新激活旧判断。新的明确接受可以覆盖自动边抑制。
- 相互矛盾的 accept/reject 不按时间静默覆盖。新判断失败时，不增加判断事件或候选版本；需要先明确撤销旧判断。
- 未发布候选的实体不能通过普通企业查询、指定 revision 查询或旧 ID 查询访问；候选版本的治理元数据仍可在管理历史中展示。
- 两个竞争发布者只能有一个成功；过期输入/事件、缺失或损坏的完整产物不能替换当前指针。
- 初始化只识别空库、已发布 `0001` 结构和当前 `0002` 结构。未知结构不会被随便 stamp，已有数据不会被清空。SQLite 使用 `BEGIN IMMEDIATE`，PostgreSQL 使用事务级 advisory lock 序列化初始化；workspace 行使用冲突忽略插入。
- 源字段变化会明确追加 EXPIRE，旧记录版本不覆盖。撤销预览不改变发布指针，历史读取使用冻结产物。

## 初始化与升级

安装新版本后，命令保持不变：

```sh
entitybridge init-db
```

默认使用当前工作目录下的本地演示数据库。已有服务通过 `DATABASE_URL` 指定数据库时，仍使用该配置。项目内便携 PostgreSQL 的调用为：

```sh
python -m entitybridge.cli init-db --portable-postgres
```

两条命令都调用随 wheel 打包的 `entitybridge.database.initialize_database`，不依赖源码目录里的 `migrations/`。升级逻辑先做元数据比较，只有确认兼容已发布结构才执行变化并写入 `0002`；未知结构或版本与结构矛盾时拒绝自动处理。

使用源码仓库且数据库已经有可信 Alembic 版本戳时，也可通过 `ENTITYBRIDGE_DATABASE_URL` 配置连接后执行 `python -m alembic upgrade head`。不要对未知数据库手工 stamp 来跳过结构核验。首次交付但尚未 stamp 的数据库直接使用上述 `init-db` 即可。

## 实际验证

```sh
python -m pytest tests/test_store_regressions.py tests/test_store.py tests/test_identity.py tests/test_resolution.py tests/test_incremental.py tests/test_oracle.py tests/test_recovery.py -q --tb=short
```

本次该命令得到 **49 passed、2 skipped**（3.41 秒）；跳过的是需要显式启用的两个 PostgreSQL 恢复用例。独立小图 oracle 和 Hypothesis 原有测试包含在这些结果中。

随后在 PostgreSQL 17.11 的 `entitybridge_test` 中，为每个用例建立随机测试 schema，启用 PostgreSQL Store 与恢复验证，运行 `test_store.py`、`test_store_regressions.py`、`test_recovery.py` 得到 **22 passed**（7.19 秒）。其中 17 项实际访问 PostgreSQL；5 项是同命令中的 SQLite/独立辅助用例。这是重叠验证，不能与前述 49 项相加宣称 71 个独立测试。测试结束仅移除随机测试 schema。出现一条 pytest 缓存目录写权限警告，用例本身全部通过。

后续 HTTP 集成发现并修复评分行状态问题后，运行 `test_resolution_regressions.py`、`test_hybrid_api.py`、`test_resolution.py`、`test_incremental.py`、`test_oracle.py`、`test_store_regressions.py`、`test_store.py`，得到 **50 passed**（7.08 秒），另有两条第三方弃用提示。独立 Boolean 可达性 oracle 的参考规范同步新增人工约束优先的显示语义，并增加 150 个 review-only/自动标志切换案例，比较全量、正常预算增量与零预算全量兜底。所有真实匹配评测数字与冻结 JSON 保持原样。

## 仍然成立的限制

- 当前 lineage 连接相邻发布版本。一个身份全部记录消失后，再观察到相同源内键，会创建新身份；旧身份不会自动连接到新身份。此时旧 ID 仍显示没有当前去向。系统没有凭源键复活就断言是同一个法律主体；若业务需要这种恢复，应增加带证据的显式身份恢复操作。
- 人工事件写入与候选产物准备分为两个事务。候选准备失败时，判断仍是可审计的已记录事件，当前发布版本不变；可重新计算发布。尚未提供持久后台任务与失败重试管理，HTTP 超时重试也没有独立的请求幂等键。
- 当前关系表、全量版本 JSON 和内存图适合单机工程验证。多租户隔离、任务队列、分页持久查询索引、长期备份与磁盘容量治理仍需专门建设。
- 保留 `branch_of` / `successor_of` 关系不等于完整法律关系规则引擎。这些关系本身不产生 same-as；名称评分与人工判断的领域约束仍需按实际业务确定。
- SQLite 是本地演示/回归引擎，不能代表 PostgreSQL 的全部事务行为。已验证的进程终止恢复不等于断电、文件系统损坏或跨节点故障恢复。
- 回归测试证明所列案例与小图随机样本符合契约，不构成“无 bug”或形式化正确性证明。

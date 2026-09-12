# ADR 008：按固定发布版本建立关系查询投影

状态：已实现；SQLite 性能实测、SQLite/PostgreSQL 正确性验证通过。

## 实际问题

v0.2 的企业列表先读取完整版本 JSON、计算 SHA-256、解码所有记录与评分边，再扫描名称和别名，最后在 Python 中分页。返回 100 个实体仍支付整个版本的读取和解码成本；企业详情同样需要完整产物。

本次先固定 10,000 条合成来源记录、5,000 个两成员实体，生成一份 11,467,079 字节的完整产物。旧路径每次读取全部 JSON。修改前每场景 25 次的初始基线为：

| 场景 | p50 / p95，毫秒 |
| --- | ---: |
| 第一页，100 个实体 | 70.60 / 73.26 |
| 深分页，offset=2500 | 69.34 / 75.84 |
| 按别名包含查询 | 79.27 / 81.95 |
| 无匹配结果 | 94.61 / 111.84 |

100 次请求累计读取约 1.147 GB 的**逻辑文件字节**。这是进程调用 `read_bytes` 的计数，操作系统缓存可能满足读取，不能把它表述为物理磁盘 I/O。

## 决策与结构

保留完整 JSON 作为冻结解释依据，新增可重建的关系查询投影。通过模块 `entitybridge.query_projection` 隔离派生数据的构建与核验，不向 source record 或判断事件中混入查询状态。

| 结构 | 内容与访问方式 |
| --- | --- |
| `query_projection` | 每个 revision 的投影版本、绑定的产物 SHA-256、投影内容摘要、实体/成员/搜索值数量；这行是完成凭证 |
| `query_entity` | `(revision_id, entity_id)` 主键和冻结 canonical 字段；列表只读取当前页的实体 |
| `query_search_value` | 按实体分开的名称/别名 casefold 值；复合主键索引限定 revision 和实体 |
| 已有 `entity_membership`、`record_version` | 复用固定成员与源版本；新增 `(revision_id, entity_id, record_id)` 索引，详情连接历史源版本，不读取当前版本代替过去 |

新候选的投影与 revision、成员、lineage 在同一数据库事务中写入，完成凭证最后写入。发布前继续验证完整产物哈希，并将 SQL 中的 canonical、搜索值、全部成员及源版本逐项与产物比较，同时核对内容摘要和数量。损坏或缺失会拒绝发布，当前指针不变。

普通查询从已发布 revision 开始，只信任完整写入并已验证的 SQL 投影，**不再每次重读并重新哈希完整 JSON**。这改变了查询的校验边界：关系数据库的事务完整性承担日常读取；管理员仍可显式全面复核或从冻结产物重建。固定版本导出及模型/治理重算继续使用原始完整产物。

## 查询契约

```python
store.entities_page(query="", revision=None, offset=0, limit=100)
# {"revision_id": ..., "total": ..., "offset": ..., "limit": ..., "items": [...]}
```

`items` 与原 `Store.entities` 的字段、成员顺序和 UUID 排序相同。当前版本只在请求开始时选择一次，后续计数与结果都固定到该 revision。没有发布版本时返回空列表和 total=0；不能通过显式 revision 访问未发布候选。

`Store.entities` 保留返回全部结果的兼容接口；`Store.entity` 保留 current/historical/retired 及旧 ID 多去向语义。企业详情使用不可变成员版本连接源记录。旧 ID 查询仍遍历已发布 lineage，其长历史查询成本不在本次分页性能承诺内。

名称搜索保持原来的 **Python casefold 后 literal substring** 语义。`%`、`_` 和 SQL escape 字符按字面字符匹配；一个实体的各个字段单独存储，不通过拼接字段产生跨字段假匹配。没有改为词搜索、模糊搜索或全局 top-k。

任意包含查询目前仍需检查所选版本的精简名称值；普通 B-tree 不会把 `%term%` 变成对数复杂度的子串检索。本次减少了整份产物解码与多余字段搬运，尚未引入 PostgreSQL `pg_trgm` 或 SQLite 全文扩展。深 `OFFSET` 也仍有与偏移量相关的工作，不能宣称任意规模恒定时间分页。

## 升级、回填与维护

迁移 `0003` 只添加投影表和成员查询索引，`0001`、`0002` 及旧 JSON 保持不变。随 wheel 分发的初始化模块识别已知 0001/0002 结构后升级；未知结构拒绝自动 stamp。空库直接创建当前结构。原命令保持：

```sh
entitybridge init-db
```

旧版本在第一次查询时校验原始产物并原子回填。两个并发读者可以各自读取同一旧文件，但仅有一个事务写入有效投影；第二个事务复用完成凭证。后续查询不读完整文件。新代码创建的版本若丢失其投影，不会被伪装成待回填旧版本，而是明确报错。

维护接口：

```python
store.projection_status(revision_id)               # 轻量完成凭证/产物绑定检查
store.projection_status(revision_id, verify=True)  # 重新核对完整产物及全部 SQL 投影内容
store.rebuild_query_projection(revision_id)        # 从已验证产物原子重建派生数据
```

轻量 ready 状态不是每次全量内容审计。重建不改变源事实、身份、判断事件、当前发布指针或原产物。写入或验证失败时整笔投影事务回滚。回填和重建使用现有 workspace 写锁，因此大版本的首次回填会暂时等待其他状态写入；后续读请求没有该写锁。

## 成对性能结果

实现后使用**同一个 revision、相同产物哈希、相同查询和分页参数**交替执行旧路径与新路径，每场景 25 对。旧路径在脚本中保留原始读取/筛选逻辑，避免拿不同输入或不同结果作比较。每对结果都比较完整响应，包含 revision、总数、分页参数、成员和 canonical 字段。

SQLite 本机结果，单位毫秒：

| 场景 | 旧路径 p50 / p95 | 查询投影 p50 / p95 | 投影完整 JSON 读取 |
| --- | ---: | ---: | ---: |
| 第一页 | 64.07 / 67.42 | 2.09 / 2.68 | 0 |
| 深分页 | 60.12 / 78.78 | 2.09 / 2.68 | 0 |
| 别名包含 | 72.75 / 94.86 | 17.22 / 22.04 | 0 |
| 无匹配 | 74.44 / 84.97 | 13.61 / 16.11 | 0 |

旧版本首次查询包含一次回填，耗时 **495.42 ms**，读取完整 JSON 一次；单独计量，没有混入稳定查询后声称首次请求同样快。稳定查询四组共 100 次完整 JSON 读取为零。时间包含应用查询过程，使用已预热的进程/操作系统缓存；p95 使用 nearest-rank。新旧路径交替顺序减少固定先后顺序偏差，但这些数字不是生产 SLO，也未推断 PostgreSQL 有相同倍数收益。

实际 SQLite `EXPLAIN QUERY PLAN`：列表使用 `query_entity` 的 revision 主键索引；包含搜索的相关子查询使用 `query_search_value` 的 `(revision_id, entity_id)` 主键索引。查询计划同时印证包含查询仍有筛查，而不是隐藏的完整 JSON 扫描。

可复现命令：

```sh
python scripts/check_query_performance.py --workdir artifacts/query-perf-new-run --records 10000 --repeats 25 --stage baseline
python scripts/check_query_performance.py --workdir artifacts/query-perf-new-run --records 10000 --repeats 25 --stage compare
```

脚本不覆盖已有报告。原始本机证据分别保存在 `artifacts/reports/query_projection_10k_v1/baseline.json` 和 `compare.json`，记录每次样本、逻辑文件读取计数、产物哈希、结果摘要和查询计划。这份合成数据只用于查询成本比较，不是实体匹配精度评测。

公开证据快照：[修改前基线](../evaluation/evidence/public_benchmarks_v03/query_10k_baseline.json)、[同输入成对比较](../evaluation/evidence/public_benchmarks_v03/query_10k_compare.json)。公开文件保留原始实测数值；本机 `artifacts` 路径继续作为运行和复现位置。

## 回归证据与范围

`tests/test_query_index.py` 覆盖与完整 JSON 查询的差分、Unicode 和字面通配符、分页顺序/总数、跨成员别名、历史成员版本、准备版本不可见、五类投影损坏拒绝发布、已升级旧版本并发回填，以及重建验证失败的事务回滚。

连同 Store 和恢复测试，SQLite 专项 **35 passed、2 个 PostgreSQL 用例未启用而跳过**；启用实际 PostgreSQL 17.11 后同组命令 **37 passed**，其中 32 项实际访问 PostgreSQL，5 项属于同命令内的 SQLite/独立辅助验证。两组有重叠，不能相加宣称独立用例总数。两种数据库均完成 0001→0002→0003 迁移往返验证。

随后补充“投影写入之后、prepare 事务提交之前失败”的故障点，SQLite 和 PostgreSQL 分别通过，验证整笔候选版本/匹配运行/投影记录回滚。查询专项当前共 16 项；上述 35/37 是补充该项之前的实际运行计数，保留原记录，不混加重复执行结果。

此次没有重训模型，没有改候选规则，也没有改动任何既有匹配评测 JSON 数字。新增索引复制 canonical 和搜索值，会增加数据库磁盘占用与准备/发布成本；这些是为降低重复查询成本付出的明确代价。异步任务队列、多租户、任意子串专用索引和完整断电恢复仍不在本次实现范围。

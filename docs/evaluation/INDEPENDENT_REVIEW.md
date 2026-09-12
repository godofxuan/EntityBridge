# 独立算法与评测审查

审查日期：2026-09-12。审查人：独立 matching 子任务。此次仅阅读代码、运行合成小实验和对已评测的旧 v3 数据做性能复验，没有读取 `v4_release` 新测试集，没有修改实现、测试或封存报告。下文保留最初审查发现；文末“修复复验”记录主任务修复后的独立结果。

## 结论

最初发现 4 项有实际复现的缺陷，其中标准化版本遗漏会导致增量结果与全量结果不同，实验入口缺少完整性与划分核验会允许无效实验产物产生。另两项影响“冻结模型指纹”和“实际重评分量”的含义。四项现已按下述反例复验通过。当前真实数据已经经过数据构建任务的审计；此前入口能接受坏数据不等于现有真实报告已经发生泄漏。

审查时运行：

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests\test_candidates.py tests\test_matching.py tests\test_evaluation.py tests\test_incremental.py -q
```

实际结果：`10 passed in 1.66s`。下面的小实验是额外发现，说明上述测试尚未覆盖相应行为。

## 发现 1 — P1：标准化升级不会改变策略版本，增量可发布过时分数

位置：`src/entitybridge/api.py:86` 的策略摘要与 `:93` 的旧记录投影；`src/entitybridge/incremental.py:81` 的变更检测。

`policy` 目前包含模型、阈值、blocking 和 resolver，不包含 `NORMALIZATION_VERSION`、`FEATURE_VIEW_VERSION`。运行时还会用当前的 `matcher_view` 同时重新投影历史原始记录和当前原始记录。标准化或标识屏蔽逻辑升级、原始记录没有变化时，两份重新投影的字典相同，`refresh_scores` 会把旧策略下的分数当作未改变的分数复用。默认 `verify_full=False` 时可以发布。

合成复现：在临时 SQLite 工作区导入两源 `ALPHA LIMITED` 与 `ALPHA LTD`，地址、邮编相同。先运行并发布 exact 模型，分数为 0。仅在实验内临时替换 `api.matcher_view`，模拟新版标准化把 `LIMITED` 改成 `LTD`，再次增量匹配并发布，然后与当前视图的全量评分比较。

实际输出：

```json
{
  "normalization_change_policy_unchanged": true,
  "refresh": {
    "changed_records": [],
    "bucket_records": 0,
    "rescored_pairs": 0,
    "unchanged_pairs": 1
  },
  "incremental_score": 0.0,
  "full_score": 1.0,
  "incremental_entities": 2
}
```

修复建议：把标准化和特征投影版本纳入完整策略摘要，版本变化必须走全量评分和归并。历史版本最好保存当时使用的匹配视图或它的内容摘要，避免用新版代码重新解释旧特征。回归测试应覆盖原始记录不变、投影版本变动的发布流程。

## 发现 2 — P1：封存实验入口接受错误文件哈希及跨划分实体

位置：`scripts/run_experiment.py:46` 开始读训练集，`:47` 接受任意 `--frozen-model`，`:67` 仅计算 manifest 本身的哈希，`:73` 开始读测试特征。

脚本没有验证 manifest 声明的每个输入文件哈希，也没有要求 evaluator 确认三个划分的实体互斥、记录 ID 唯一以及特征/真值覆盖一致。冻结模型的元数据也没有训练特征内容哈希，无法证明 `--frozen-model` 来自本实验允许的训练数据。即使上游正常构建器已经做了部分检查，命令行入口仍可接受不符合协议的数据或来源错误的模型。

合成复现：临时生成 train、validation、test 各 2 条记录，三组的 `true_entity_id` 都设置成同一个值；在 `manifest.files` 中把 `matcher/test.parquet` 的哈希写成 `deliberately-wrong-sha256`；使用现有合成兼容夹具保存的模型运行完整脚本。模型没有接触任何真实新测试数据。

实际输出：

```json
{
  "experiment_completed": true,
  "known_entity_spans_three_splits": true,
  "manifest_contains_deliberately_wrong_file_hash": true,
  "test_records_in_output": 2
}
```

修复建议：在训练前验证 manifest 全部输入哈希，由 evaluator 做实体/记录划分完整性检查，matcher 只收到通过检查的特征数据。检查可以读取划分元信息，不需要计算或查看测试效果。保存模型时记录训练特征内容哈希和数据/特征版本，使用冻结模型时强制比对；不要仅凭记录数或用户传入的目录名相信训练来源。将整个实验脚本本身也纳入代码版本摘要，当前 `code_sha256` 只涵盖 `src/entitybridge/*.py`，无法识别阈值网格等实验脚本修改。

## 发现 3 — P2：可变模型对象与缓存指纹不一致

位置：`src/entitybridge/matching.py:64` 保存外部可变对象，`:68` 使用 `cached_property`，`:147` 每次读取当前 settings。

`settings`、`tf_name`、`training_metadata` 都公开且没有深拷贝。第一次读取 `fingerprint` 后修改任一对象，评分会采用新值，但指纹仍保持旧值。`save()` 也可能写出内容和缓存指纹不匹配的模型，之后 `load()` 才报错。当前正常加载后立即评分的路径没有主动修改这些对象，但类本身提供的“冻结”保证不成立。

合成兼容模型复现：先评分并读取指纹，再执行：

```python
model.settings["probability_two_random_records_match"] = 0.5
```

实际结果：`fingerprint_unchanged_after_mutation=true`；同一对记录的分数从 `0.9993909961170092` 变为 `0.999984375244272`。

修复建议：深拷贝输入并保持私有不可变状态，对外只返回副本；所有评分与保存使用相同的冻结内容。另一方案是在评分/保存前重新验证完整摘要，但不能为每条边重算大 TF 表摘要。回归测试应包含构造器外部参数变更、嵌套 TF 行变更和设置属性变更。

## 发现 4 — P2：增量报告的重评分对数小于实际 Splink 评分量

位置：`src/entitybridge/matching.py:136` 至 `:158`，`src/entitybridge/incremental.py:97`。

`score(records, candidates)` 先对 `records` 内满足四条 blocking 的所有记录对执行 Splink `predict()`，生成比较和分数后才过滤 `requested`。当它被增量刷新调用时，局部桶中未改变的记录对也会被重新评分；报告却把请求候选数写成 `rescored_pairs`。因此它返回的边集合可以正确，但工作量指标与“只重评分变更端点”的说明不一致。

合成复现：两源各 2 条相同名称、地址和邮编的记录，只有 1 对放入 `score` 的 candidates 参数。对真实 `LinkerInference.predict` 包装只读行数计数，结果为：

```json
{"requested_pairs": 1, "actual_predict_rows": [4]}
```

修复建议：在比较/评分之前过滤请求对，保持等值 blocking 以避免全表笛卡尔积。主任务提出的 DuckDB 成对 UDF 过滤方案可以验证，但应检查实际查询计划仍保留等值连接，并用 predict 实际产出的行数断言请求数；仅断言最终返回 1 对不足以防止回归。若暂不调整算法，应报告真实比较行数而不是请求数。

## 已确认行为与界限

- 当前候选 v2 对缺失 country 的处理和 Splink SQL 一致；本审查没有把此前已修复的 v1 国家缺失漏召回重新列为缺陷。
- `register_term_frequency_lookup` 在每次预测前注册训练集 TF 表，未发现预测时主动更新该表的路径。现有保存/重载/添加无关记录测试通过。此前未见名称在固定 TF 表内不存在时，Splink 使用缺失 TF 的回退行为；这不等于从新输入重新学习 TF，也不代表分数已校准。
- `known_pairs` 把同一实体所有记录对作为正例。当前 A/B 构建器每实体恰有两条跨源记录，因此候选 recall、条件 recall 与端到端 recall 的分母一致且包含漏候选。若日后把多快照或同源别名作为独立记录，必须给 pair 指标明确跨源资格，否则同源正例也会进入 link-only 模型无法召回的分母。
- B-cubed 实现按每条记录计算预测簇与真值簇交集，并要求完整真值覆盖；在当前一条输入记录只出现一次的 resolver 输出上未发现分母错误。
- `recompute` 将旧新所有候选边、约束、抑制关系与旧簇纳入闭包，保留此前被冲突阻止的边。现有小图属性测试通过。它仍然扫描全图构建闭包；“局部归并”不等于总耗时只依赖受影响记录数。
- 候选索引的时间/内存受全部桶的跨源乘积总和影响，单桶预算没有限制全部候选总数；训练阶段 postcode/name EM 也不受候选模块的单桶预算约束。已完成的 100k 档位是特定数据分布下的实验，不能扩展为任意规模/大桶的完成保证。
- 字段证据目前只输出 `bf_name`，没有输出实际参与分数的 `bf_tf_adj_name`。这不改变分数，但解释页面无法完整还原名称字段的贡献，建议把 TF 调整单独展示。

## 审查版本标识

下面的哈希属于发现时读取的实现，修复后行号和哈希会变化。

| 文件 | SHA256 |
|---|---|
| `src/entitybridge/incremental.py` | `cac5139baeae17c39babcd3a82b7007c5d0bbbeb99e968b73d63f8ee080494ad` |
| `src/entitybridge/matching.py` | `c55eeb5d525b1695a1c4e7ceee5af25bfb96e84314e60c1ecb519f1e29eb1672` |
| `src/entitybridge/candidates.py` | `5fc00e819ccc15393d60e9892597b03b4678ce4a0987a3c3ff1e9d6030e80dd6` |
| `scripts/run_experiment.py` | `d5b5d9ca0b9fb9d911fd1fe7068d18d2c50e64c9e9af5e96632808dd393ea549` |

## 修复复验

复验日期：2026-09-12。本节由独立审查任务追加，使用原最小反例，并以旧 `artifacts/datasets/v3/real_100000/matcher/` 做实际性能验证。没有读取任何 v4_release 文件；性能运行不读取 truth_map，不选阈值、不计算新匹配效果，也不覆盖任何旧产物。

### 四项缺陷的状态

| 原发现 | 复验方法与实际结果 | 状态 |
|---|---|---|
| 1：标准化版本遗漏 | 原 ALPHA LIMITED / ALPHA LTD 反例，同时模拟新投影与 `NORMALIZATION_VERSION` 更新。策略摘要变化，`score_refresh=null`，计算模式 `full`；发布后分数 1.0，与全量相同，归属 1 个实体。原来的旧分数 0.0 不再被复用。额外工作流测试也覆盖 FEATURE_VIEW_VERSION 变更。 | 通过 |
| 2：实验入口校验缺失 | 通过真实 `run_experiment.main()` 分别提交错误输入哈希、跨划分实体、特征/真值覆盖不一致、冻结模型训练特征哈希错误。四种情况都抛出相应 ValueError，均没有生成 frozen_config.json 或 report.json。正确的 6 行合成数据 preflight 通过，报告 4 文件、3 个实体、划分互斥。 | 通过 |
| 3：缓存指纹与可变状态 | 分别修改构造函数传入的原始 settings、TF 首行、嵌套 metadata；再修改公开属性返回的副本。原模型的分数和指纹都不变，保存/重载指纹一致。 | 通过 |
| 4：请求数与实际评分数不一致 | 原两源各 2 行同名同址反例，只请求 1 对；独立包装真实 `LinkerInference.predict` 读取其结果行数，得到 1；模型的 last_prediction_count 和返回结果也是 1。旧实现此处实际生成 4 行。 | 通过 |

发现 1 的保证以修改标准化实现时同步更新版本号为前提。发现 3 的修复通过深拷贝和私有状态隔离正常公共接口的变更；故意访问下划线私有成员不属于此 API 的冻结契约。

入口拒绝原因实际为：

```text
Dataset hash mismatch: matcher/test.parquet
A true entity occurs in more than one split
Matcher IDs do not exactly cover evaluator split IDs
Frozen model training provenance does not match the permitted train split
```

### DuckDB 计划与 100k 路径

在 `DuckDBAPI._execute_sql_against_backend` 外包只读追踪器，捕获实际包含 `eb_requested_pair` 的 `CREATE TABLE __splink__blocked_id_pairs... AS ...`，对同一 SQL 执行 `EXPLAIN`。没有替换生产 SQL 或模拟评分。

小样与完整 100k 查询计划都保留四条 blocking 的 `HASH_JOIN`。连接条件仍包括名称相等、名称前 8 字符相等、邮编加名称前缀相等、地址加邮编相等。UDF 是候选过滤条件，未观察到 `CROSS_PRODUCT` 或 `BLOCKWISE_NL_JOIN`。

| 实际运行项 | 观测值 |
|---|---:|
| 旧 v3 源记录数 | 100,000 |
| 唯一候选对 | 914,580 |
| 实际 predict 行数 | 914,580 |
| 实际返回评分数 | 914,580 |
| 输入读取耗时 | 0.120 秒 |
| 候选生成耗时 | 5.935 秒 |
| 完整 score 调用耗时（含 EXPLAIN） | 18.385 秒 |
| 进程峰值工作集 | 3,841,761,280 字节 |
| 实际计划 HASH_JOIN / CROSS_PRODUCT / BLOCKWISE_NL_JOIN | 4 / 0 / 0 |
| 同一 100k 输入只请求 1 对：实际 predict / 返回 | 1 / 1 |
| 同一 100k 输入只请求 1 对：score 耗时 | 1.707 秒 |

此次评分复用 `artifacts/reports/baseline_100k_v2/model`，仅检验现有评分执行路径的行数和成本，不声明该模型对 v3 数据的新效果。模型保留原有“name 的部分 u 层未训练”提示，没有为了跑通实验屏蔽该事实。

这些观测排除了本次 UDF 修复在实际 100k 路径上把连接退化为全表笛卡尔积的担忧。只请求 1 对仍需扫描输入和检查 blocking，因此不意味着 100k 输入的总工作量变成常数；单个大桶的固有跨源乘积也仍存在。

### 回归命令

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m pytest tests\test_candidates.py tests\test_matching.py tests\test_evaluation.py tests\test_incremental.py tests\test_workflow.py -q
```

实际结果：`13 passed in 2.56s`。

复验实现的 SHA256：

| 文件 | SHA256 |
|---|---|
| `src/entitybridge/api.py` | `68a8be5182101f2f1afc81f913f004634840c9b9b7d67af710df13eef6dd7485` |
| `src/entitybridge/matching.py` | `a7869c645ac803e7a07f7ea41ba56788fa7297cfd1380c82d4f2ca437fe59ebc` |
| `src/entitybridge/evaluation.py` | `9151cea685bb8974e01fb5839152208f6329e11562c61eb89ce55c0c12475ce1` |
| `scripts/run_experiment.py` | `784912ee7da328a7b358e575cc32d3631f7426afd23c4073be38e4ced59c8fbf` |

前次附带建议中，完整代码摘要纳入实验脚本、字段证据输出 `bf_tf_adj_name` 仍可继续完善，不影响本节对四项原始反例的关闭结论。新增 preflight 会读取测试文件的哈希及 ID/划分元信息；封存配置里的 `test_read_after_config_write` 应理解为配置冻结后才读取测试特征进行评分，建议后续重命名以准确表达这一边界。

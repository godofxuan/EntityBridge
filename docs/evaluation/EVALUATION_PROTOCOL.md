# 显式部分标注配对的评测协议

协议日期：2026-09-12。实现位于 `src/entitybridge/benchmark_metrics.py`；基准来源、字段投影与切分规则见[公开基准契约](../data/PUBLIC_BENCHMARKS.md)。本协议不覆盖此前完整登记号真值的结果，不重写旧报告。

## 先确定能回答的问题

部分标注数据只告诉我们：**这些明确给出标签的配对**是同一对象或不同对象。未列出的配对为 unknown，不能因为“没有正例标签”就当成负例，也不能从已知正边的连通分量推导出完整实体真值。

因此首页只需要三组主要证据：

1. 候选对已知正例的覆盖，以及需要比较的候选数量。
2. 明确标注配对上的端到端 precision/recall/F1，旁列候选内结果和排名 Average Precision。
3. 固定复核预算能找到多少已知正例，未知候选是否占用了预算。

ROC-AUC、校准诊断和分组区间用于解释结果；它们不替代主指标，也不把部分标注样本变成真实业务总体。Splink 官方同样区分模型、边与簇的评估层次。[Splink 评估说明](https://moj-analytical-services.github.io/splink/topic_guides/evaluation/overview.html)

## 输入与切分边界

匹配器仍只接收八列：`record_id, record_version_id, source, name, address, city, postcode, country`。评估器单独接收 `evaluator/labelled_pairs.parquet`：

| 字段 | 语义 |
|---|---|
| `left_id`, `right_id` | 同一无向配对的两个不同记录 ID；统一按字符串排序 |
| `label` | 显式整数 `0` 或 `1`；未知配对不创建伪标签 |
| `split`, `original_split` | 当前实体隔离切分与原发布切分的出处 |
| `left_group_id`, `right_group_id` | 用于隔离记录的已知正例闭包，不等于完整真值实体 |
| `group_id` | 当前 split 保留的全部正、负标签边的连通分量，作为依赖分组 |

重复配对、冲突标签、非有限分数和缺失评分均明确报错。候选集与评分映射必须逐对一致，防止失败的评分被静默删掉，从而夸大结果。没有候选的正例仍保留在端到端 recall 分母里。

按已知正例闭包隔离 train/validation/test；跨当前 split 的标签对按适配协议丢弃并计数。切分依据可以使用 evaluator 标签检查隔离，但这些标签和闭包 ID 不进入候选或模型特征。由于标签有限，这只保证已知实体关系不跨分区，不能证明所有未知真实实体均隔离。改变官方原切分后，应报告自己的切分，不能直接与官方排行榜数字排名比较。

只在 train 拟合规范化之外的可学习参数、词汇/IDF、监督模型与校准器。validation 选择阈值；配置落盘并冻结后，runner 才读取 test 特征、评分并计算效果。文件哈希和 ID 完整性预检不属于使用 test 效果调参。已经看过的测试集再次评估必须标记 exploratory reuse；不能把再次运行称为全新未见测试。

## 候选、配对与排名

设显式正标签集合为 `L+`、负标签为 `L-`，候选集合为 `C`，达到冻结阈值的预测为 `P`，且 `P ⊆ C`。

- 已知正例候选覆盖：`|C ∩ L+| / |L+|`。它是相对于已标注正例的 pair completeness，不是未知总体的召回保证。
- 候选 reduction ratio：`1 - |C| / |U|`。双源链接的可比较全集大小通常为 `n_A × n_B`；单源去重为 `n(n-1)/2`。必须显式给出正确的全集，否则不计算比例。不要用“标签总数”充当全量搜索空间。[Record Linkage Toolkit 的定义](https://recordlinkage.readthedocs.io/en/latest/ref-evaluation.html)
- 端到端：`TP=|P∩L+|`，`FP=|P∩L-|`，`FN=|L+\P|`。未知预测单列数量，不计入 FP。
- 候选内：只把 `L+∩C` 和 `L-∩C` 作为分母。与端到端的差异说明 blocking 漏失，不能只展示这一较容易的结果。

precision 没有已标注预测时返回 `null`；recall 没有正例时返回 `null`；F1 使用 `2TP/(2TP+FP+FN)`，分母为零才返回 `null`。例如有正例却全部拒绝，precision 未定义、recall 和 F1 为 0。这比把所有空值都写成 0 更容易区分“没有证据”和“确实漏掉”。

排名指标只在**已标注且被候选覆盖**的评分上计算；unknown 不参与排名指标，候选遗漏另由端到端 recall 揭示。主要返回非插值 Average Precision：按 recall 增量加权 precision；辅助返回明确命名的 `pr_auc_trapezoid`，避免把两种面积混称同一值。[scikit-learn AP 定义](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.average_precision_score.html)

正负比例、标签抽样及候选策略都会改变这些数值。高度不平衡时，仅看 ROC-AUC 可能掩盖大量误报对 precision 的影响；不能据 ROC 很高推导出低人工负担。[Saito 与 Rehmsmeier 原论文](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0118432)

| 样本情况 | AP / PR 面积 | ROC-AUC |
|---|---|---|
| 没有已标注候选 | `null`，给出原因 | `null` |
| 只有负例 | `null`，没有正例 recall 曲线 | `null` |
| 只有正例 | 1，但明确单类，不能证明区分能力 | `null` |
| 正负都有 | 正常计算，分数相同时一起处理 | 正常计算 |

**`labelled_pair_only` 是分类诊断。** 如果直接把所有标签对送给模型，已知正例候选覆盖必然为 1；这是人为提供候选后的评分结果，不是可运行检索系统的真实召回。必须与 fixed/hybrid 候选结果分开展示。

## 复核预算与阈值成本

`review_budget_curve` 按分数从高到低排列**全部候选**，同分按记录 ID 稳定排序。每个已知正例、已知负例和 unknown 都花费一个预算单位。输出实际审查数量、已知正例捕获、已知负例/unknown 数量、标签覆盖比例，以及预算边界处同分候选数量。

例如最高分是一条 unknown，预算为 1 时仍然消耗一次审查，但已知正例捕获为 0；不能先删除 unknown 再声称只审一条就找到一个正例。这个曲线也不等于节省多少人时：每条的审查难度、未知项真实标签和人工错误尚未测量。分数相同时，ID 的排序仅为复现约定，不代表有效的审查优先级。

`select_cost_threshold` 只接受 `split="validation"`，并拒绝标签行中出现其他 split。默认检查所有实际分数阈值和“全部拒绝”；最小化 `FP_cost×FP + FN_cost×FN`，其中包括未召回的已知正例。相同成本优先全部拒绝，再优先较高阈值。全部拒绝使用 JSON `null` 阈值和明确决策规则表示，避免无穷大数值。

调用者也可以显式传入预声明的阈值集合。当前公开基准 runner 使用已标注 validation 候选的分数和“全部拒绝”，避免为不改变已知 TP/FP 的未知项分数写出巨大曲线；选择范围仍然只来自 validation。

FP:FN=10:1 是一次**事先声明的相对成本假设**，不是通用业务标准或真实货币损失。未知预测不加入已知成本，但单列数量；若标签样本通过难例筛选或正负采样取得，成本也只描述这一样本。缺少正例或负例时不选择有效阈值，返回 `insufficient_class_support`。函数的 split 检查不是文件权限隔离；runner 仍须保证没有传入伪称 validation 的 test 数据。

## 概率校准不是相似度校准

只有调用者明确声明 `score_kind="probability"`，且全部分数属于 `[0,1]`，才计算 Brier 与 ECE。Jaro–Winkler、余弦相似度或未赋予概率含义的打分即使也在 `[0,1]`，也不应请求概率校准指标。

- Brier 使用二分类定义 `mean((p-y)^2)`，范围 `[0,1]`。它是概率预测的整体损失，还受区分能力影响，不能单独证明校准良好。[Brier 官方定义](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.brier_score_loss.html) · [概率校准说明](https://scikit-learn.org/stable/modules/calibration.html)
- ECE 使用 10 个固定等宽的**正类概率**区间，计算各箱平均预测概率与正例比例差的加权绝对值，最后一箱包含 1。输出每箱样本数；空箱不贡献误差。不同分箱和样本量会改变 ECE，因此不能挑最有利的箱数。[ECE 方法背景](https://proceedings.mlr.press/v70/guo17a.html)

这两个指标同样只基于已标注候选。经过 case-control 抽样的正例比例未必等于实际流量；仅在这些样本上 Brier/ECE 小，不能宣称概率对总体已校准。本次指标函数不自动拟合校准器，更不在 test 上拟合。

## 为什么按组 bootstrap

共享记录或真实实体的多条 pair 往往相关。独立重采样每条 pair 会夸大独立信息量；重采样应尊重数据的层次结构。层级数据研究也强调匹配采样结构的重要性；这里采用的具体连通分组和拒绝阈值是本项目的保守评估约定，不是从其他领域照搬的覆盖率保证。[层级 bootstrap 研究](https://pmc.ncbi.nlm.nih.gov/articles/PMC7906290/)

本实现用当前 split 全部保留标签边的连通分量作 `group_id`；同一 record ID 出现在两组时拒绝计算。每次有放回抽取同样数量的完整组，保留组内所有 pair 及其数量，再汇总 TP/FP/FN；**不先平均每组 F1**。阈值和模型在所有重采样中固定。

默认 1,000 次、固定随机种子、95% percentile 区间，覆盖候选已知正例 recall 与端到端 precision/recall/F1。返回真实组数、最大组占标签比例和 `N²/Σn_g²` 的组大小均衡诊断；后者不是真实独立样本量的证明。

- 少于两组，或者单一依赖组超过全部标签的 50%，返回 `null` 区间与原因。
- 某指标的点估计未定义，或不足 95% 重采样中有定义，该指标也不返回区间，避免大量丢弃单类/零预测样本后产生虚假的精确区间。
- 少于 20 组明确提醒 percentile 区间不稳定。即使更多组，也只是在假定组之间独立条件下的描述性区间。

这种 bootstrap 不修复标签选择偏差，也不涵盖训练、阈值选择、潜在未知实体跨组、未来时间变化的不确定性。要比较两种方法的显著差异，应使用同组同次抽样的**差值**区间，不能简单判断两个独立区间是否重叠；本轮函数不自动给出显著性结论。

## 完整簇指标何时适用

有完整实体真值且预测覆盖同一记录全集时，可补充以下指标。只有部分 pair 标签时，不能把未知关系补成不同实体再计算它们。

| 指标 | 关注点与限制 |
|---|---|
| 配对 P/R/F1 | 解释直接，但大簇会产生平方量级配对，容易主导整体结果 |
| B-cubed P/R/F1 | 对每条记录比较预测簇与真值簇交集，再对记录平均；能同时反映误合并和拆散，应报告 P 与 R 而非只剩一个 F1。[原方法比较论文](https://doi.org/10.1007/s10791-008-9066-8) |
| CEAF | 对预测簇和真值簇作一对一最佳对齐，避免一个簇被重复匹配拿分；应说明采用 mention-based 还是 entity-based 相似函数。[Luo 原论文](https://aclanthology.org/H05-1004/) |
| ARI | 比较同簇/异簇关系并调整随机一致性，但它不直接表达误合并与漏合并的业务代价。[官方定义](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.adjusted_rand_score.html) |

当前部分标注模块不输出伪 B-cubed/CEAF/ARI。此前完整登记号评估继续使用自己的 `evaluation.py`；可以由其 B-cubed P/R计算调和平均，但不能混入部分标签报告。无完整真值时，簇大小、密度或桥边只能用于审查线索，不能充当准确率。[Splink 簇评估](https://moj-analytical-services.github.io/splink/topic_guides/evaluation/clusters/overview.html)

## 函数契约与验收

```python
from entitybridge.benchmark_metrics import (
    evaluate_labeled_pairs, group_bootstrap_intervals,
    review_budget_curve, select_cost_threshold,
)

# labels = parquet.to_pylist()，包含上面的显式 label/group_id 字段。
# candidates 为无向 pair 集合；scores 为 {(left_id, right_id): float}。
choice = select_cost_threshold(
    validation_labels, validation_candidates, validation_scores,
    split="validation", false_positive_cost=10, false_negative_cost=1,
    return_curve=True,
)
assert choice["status"] == "selected"  # 缺任一标签类别不能假装已选择有效策略。
# 先持久化 choice 和模型/数据摘要，再执行 test 推断。
metrics = evaluate_labeled_pairs(
    test_labels, test_candidates, test_scores,
    threshold=choice["selected_threshold"], score_kind="similarity",
    universe_pair_count=test_source_a_count * test_source_b_count,
)
intervals = group_bootstrap_intervals(
    test_labels, test_candidates, test_scores,
    threshold=choice["selected_threshold"], n_resamples=1000, seed=20260912,
)
budget = review_budget_curve(test_labels, test_scores, [0, 50, 100, 500])
```

回归覆盖未知预测不算负例、候选漏失分母、缺失评分拒绝、单类/空集 AUC、同分处理、概率语义和边界、未知项消耗预算、validation 限制、全部拒绝、整组重采样、巨组拒绝和冲突输入。运行：`python -m pytest -q tests/test_benchmark_metrics.py`。实际基准报告应保留所有预声明方法，明确样本数、来源、划分、未知项比例、阈值和不可定义指标；不根据 test 结果删掉失败方法。

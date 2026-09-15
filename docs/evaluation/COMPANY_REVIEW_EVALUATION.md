# 公司复核预算评测：数据门槛和执行协议

本轮没有提供经核验、独立于开发的公司跨源人工标签。现有登记号派生基准、旧WDC复测、六条合成治理样例各自保留原用途，不能成为新的独立公司holdout。`COMPANY_REVIEW_PROTOCOL.json` 因此是草案，dataset_sha256和adoption_threshold未填写；没有开展公司质量训练，也没有新的公司效果数值。

## 已实现的评估入口

```powershell
python scripts/evaluate_company_review.py --protocol docs/evaluation/COMPANY_REVIEW_PROTOCOL.json --output artifacts/company-quality-gate.json
```

无数据时输出明确阻塞状态，training_runs=0、quality_metrics=null。输出不能覆盖。满足数据条件后使用 `--dataset dataset.json --predictions predictions.json`；输入先检查封存协议/数据哈希、模型指纹、记录及实体跨分区互斥、重复配对、有限分数、同一候选池和未知标签。

数据JSON需要：domain="company"、synthetic=false、holdout_status="independent_frozen_before_scoring"；annotation_provenance含source_reference、usage_terms、annotation_method、review_record_reference；partitions含train/development/holdout，各记录显式给record_id和entity_id；labels每行给left_id/right_id/label（0、1或null）。entity_id用于隔离相关实体，不能用随机ID规避泄漏检查。

预测JSON需要：protocol_sha256；model_fingerprints包含baseline和ditto两个SHA256；scores的baseline和ditto各为left_id/right_id/score列表。两模型必须使用相同冻结候选集合。标签可覆盖候选外已知正例，以显式计入候选遗漏。协议须先设status="frozen"、确切dataset_sha256和非空业务adoption_threshold；结构检查不能自动认证人类标注或成本标准合理。

主预算100、辅助25/50；未知标签消耗预算但不当负例。报告候选已知正例召回、预算内已知正例数和未知覆盖，以及已知候选上的AP/梯形PR-AUC。描述性统计不自动决定采用模型。没有认证完整簇真值、独立抽样块或真人计时，因此不报告簇质量、置信区间、效率节省。校准需要单独开发集拟合和封存。

## 尚需实际完成的外部步骤

收集合规可用的跨源公司候选，包含同名异企、别名、缺失字段和随机抽样；记录采样概率与来源，配对保留unknown。由人工核验匹配关系并记录证据、标注者和分歧裁决，再按实体/依赖组分离训练、开发和封存测试。若要声明新时间段泛化，需另收后续时期数据。

在查看holdout之前冻结候选策略、业务错误成本、最低标签覆盖、采用标准和模型配置。只比较一个已有非神经基线与一个公司Ditto配置；必要时最多三种子，不作测试集调参。数据不足、泄漏、候选召回瓶颈或预算耗尽时按草案停止规则报告。当前工具验证的是结构和声明，外部人工核验不能由程序自签完成。

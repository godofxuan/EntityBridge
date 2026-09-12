# v4_release：重新封存的未评估企业测试集

## 变更理由

此前v3评测发现候选规则错误地把缺失country当成不匹配，并阻断name-only条件。相关旧报告保留；修订候选规则后不能把已经查看过结果的旧测试企业重新称为未见测试集。

本版本从同一组冻结官方快照重新扫描完整干净跨源重合，排除v3/real_100000包含的**全部**企业，覆盖其train、validation、test，而非只排除旧test。剩余企业全部进入新的test。训练和验证直接复制v3对应Parquet字节；旧test不进入v4_release。

这属于同一来源、同一时间快照内，先前未进入模型评测的企业补集，不是新的时间快照或外部业务总体。不能将它描述为时间外推或真实采购系统成绩。

## 复现

```powershell
.venv/Scripts/python.exe scripts/build_benchmark.py --raw-dir artifacts/raw --output-dir artifacts/datasets/v4_release --release-exclude-from artifacts/datasets/v3/real_100000
.venv/Scripts/python.exe -m pytest tests/test_ingestion.py tests/test_normalization.py -q
```

目标目录封存后拒绝覆盖。再次执行使用新输出目录；新记录的UUID首次随机生成并持久化于该目录的evaluator/private_registry.json。企业选择以完整登记号集合差集为准，不依赖随机UUID。

## 输出契约

- `matcher/train.parquet`和`matcher/validation.parquet`必须与v3对应文件SHA-256完全一致。
- `matcher/test.parquet`只包含先前全部50k企业的补集；不带登记号、LEI、来源URL或真值列。
- `evaluator/truth_map.parquet`保持`record_id,true_entity_id,split`契约；原文和登记信息只保存在evaluator侧。
- 相同八列allowlist和文本标识清除规则保持不变。
- `evaluator/holdout_verification.json`记录实体集合、源记录键及opaque记录ID零交集证明；manifest记录此前数据集清单和rich文件哈希、源快照哈希、规则及实际数量。

程序会拒绝此前数据文件哈希改变、特征视图版本不一致、此前企业在重新扫描总体中消失，以及不存在新企业的情况。测试覆盖“仅复制旧train/validation，旧test也从新test排除”的完整行为。

测试集构建期间不运行候选、评分或模型效果分析。实际数量与执行校验结果以manifest和holdout_verification为准。

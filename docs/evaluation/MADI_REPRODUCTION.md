# MaDI 公司公开数据研究与复现

本轮选择 [MaDI-Bench Companies](https://wbsg-uni-mannheim.github.io/MaDI-Bench/) 的 base 任务：Forbes、DBpedia、FullContact 三来源公司档案，两个有配对标签的来源组合。[原论文](https://arxiv.org/html/2606.30371v1)称 Companies 测试配对经过人工核验；本项目未进行第二轮独立人工标注。它提供公开公司匹配证据，但不能替代法律主体或用户 CRM 的业务真值。

## 数据边界

上游固定提交 `8751b856af84288fd5c490b6f66b7c3390b7e6e1`。12 个输入文件的路径、字节数和 SHA256 见 [快照清单](../data/MADI_COMPANIES_SNAPSHOT.json)。下载器只接受这组文件并逐项核验；不运行上游代码。

原始14,016条记录，标签训练1,971、验证948、测试599。结构检查在模型拟合前发现3个训练正例端点缺失、1个负例与已知正关系的传递闭包矛盾，以及1,199条来源记录跨原始划分出现。按预定规则隔离矛盾组件的11条记录及27条关联标签，并清除跨划分已知实体的训练/验证配对，保留官方有效测试。没有按模型成绩挑选样本，也没有把未知配对补成负例。

| 划分 | 记录 | 正例 | 负例 | 配对 |
|---|---:|---:|---:|---:|
| 训练 | 7,404 | 325 | 213 | 538 |
| 验证 | 3,107 | 217 | 233 | 450 |
| 测试 | 3,494 | 176 | 420 | 596 |

清理改变了原始类别比例；这是保护测试实体的研究划分，不是原论文榜单复现。只映射名称、城市、国家到现有匹配接口；URL、登记号、人员、财务信息和行业不作为模型特征。Forbes 没有映射城市，因此“至少一端缺城市”切片与整个评测重合，不应包装成独立子群发现。来源含有潜在遗漏的实体关系，互斥保证只针对已知正关系。

DBpedia元数据声明CC BY-SA 3.0、Forbes声明CC BY 4.0；FullContact元数据与标签仓库根目录未找到独立授权声明。这是对上游声明的记录，不是独立版权确认。原表、转换后记录、逐对预测和模型权重留在本地忽略目录，不随源码导出；保留来源链接、自有适配代码、哈希及汇总结果。

## 冻结与指标

[原始协议](MADI_COMPANIES_PROTOCOL.json)在首次拟合前写入，SHA256为 `222a20ca0b90b6608d4494dbeb2dd139a315c67eac4ed0e7af8451b42bb16b7b`；[实现约定](MADI_IMPLEMENTATION_NOTES.md)解释来源对内部检索、双Splink模型和成对bootstrap。两文件与全部应用Python源码、执行脚本、核心锁文件一起封存。

四条候选轨道：固定分桶、严格国家的hybrid、仅检索时放宽国家的hybrid、给定标注对的分类诊断。TF-IDF只在训练名称拟合，hybrid固定top-k=10、最小相似度0.2。后者不能代表实际候选召回。所有评分器先评相同候选并集，再切回轨道；评分阶段保留原始规范化字段。

七个评分器：精确名称、模糊名称、现有Splink配置、固定逻辑回归、Ditto种子11/23/47。Splink每个来源对分别训练，不能把它的受限字段和默认参数结果理解为该框架最优成绩。Ditto使用既有固定RoBERTa基础权重、MixDA和删除增强，最长128token、微批8、累积4、最多20轮、验证F1连续4轮无改善早停。每个种子恰好一次训练，无超参数搜索。

每个模型/候选轨道分别在验证集选最大F1与最小10FP+FN两种阈值，允许拒绝全部。全部模型和阈值一起冻结后才读取测试特征。成本10:1仅为敏感性情景，不是业务提供的损失函数。

主要比较是给定配对轨道上复核前100对的已知正例数：三个Ditto种子的均值减逻辑回归，不能挑最佳测试种子。附带报告预算25/50、AP与PR-AUC、召回遗漏计入的P/R/F1、成本、候选量、未知预测比例、来源对和国家冲突切片。按已知实体及保留标签形成的依赖组件做1,000次成对bootstrap；小于2组件或最大组件超过50%时拒绝给区间。区间条件于已拟合模型，不覆盖训练不确定性、数据集选择或未知标签。

## 执行

先按 [神经环境与基础权重](DITTO_REPRODUCTION.md#安装与权重)安装核心与神经锁文件。Python3.12、当前研究需要兼容bf16的CUDA设备；不要与别的GPU任务争抢显存。研究产物必须写入新目录。

```powershell
$env:PYTHONPATH=(Resolve-Path src).Path
$env:PYTHONUTF8='1'
$env:HF_HUB_OFFLINE='1'
$env:TRANSFORMERS_OFFLINE='1'
$env:TOKENIZERS_PARALLELISM='false'
python scripts/fetch_madi_companies.py --raw artifacts/madi_raw --output artifacts/madi_data
python scripts/run_madi_study.py prepare --dataset artifacts/madi_data --output artifacts/madi_study
python scripts/run_madi_study.py train --dataset artifacts/madi_data --output artifacts/madi_study --seed 11 --gpu-confirmed
python scripts/run_madi_study.py train --dataset artifacts/madi_data --output artifacts/madi_study --seed 23 --gpu-confirmed
python scripts/run_madi_study.py train --dataset artifacts/madi_data --output artifacts/madi_study --seed 47 --gpu-confirmed
python scripts/run_madi_study.py evaluate --dataset artifacts/madi_data --output artifacts/madi_study
```

`--gpu-confirmed`表示操作者已确认GPU时段，脚本不会替你清理其他进程。阶段不可覆盖；源码、数据或模型哈希变化会停止。测试阶段中途故障需留下原始失败证据并明确另建重放实验，不能删掉冻结标记继续伪装首次测试。首次运行结果出来后，再次执行只能称复现/重放，不再称新的未见测试。

本机正式研究目录为 `artifacts/research_20260915/study`：`pre_fit.json`、`source.zip`、`prepared.json`、各种子训练计划/逐轮历史/ready回执、`frozen_config.json`、逐对本地分数、`report.json`与`evidence_sha256.json`形成证据链。自动回归覆盖适配器、泄漏防护、候选范围、冻结阻断和共享组件抽样；微型模型测试只验证软件链路，不算真实效果实验。

## 其他公开数据的用途

- 已有GLEIF/Companies House实验提供登记信息对齐与身份治理场景，但当前名称高度一致，不能代替困难文本匹配。
- [DeepMatcher数据目录](https://github.com/anhaidgroup/deepmatcher/blob/master/Datasets.md)含餐馆、论文、商品与公司任务。既有Fodors/DBLP实验保留为跨领域诊断；其公司任务使用长文档，需要另行设计字段投影，不能直接当作地址/短名称任务。
- [WDC Products](https://webdatacommons.org/largescaleproductcorpus/wdc-products/)适合困难商品匹配与未见实体诊断，本仓库已有真实Ditto复测；它不证明公司匹配质量。

后续扩展须先写清目标、划分和指标，再用新的外部数据评测；看过本轮测试后再优化的模型不能借用同一批测试作为独立确认。

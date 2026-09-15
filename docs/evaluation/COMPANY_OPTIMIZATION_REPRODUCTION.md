# 公司优化实验复现与使用

本轮协议为 [COMPANY_OPTIMIZATION_PROTOCOL.json](COMPANY_OPTIMIZATION_PROTOCOL.json)。四次模型拟合、四个校准映射、五种候选轨道，两类验证阈值；没有新增神经训练或测试后调参。MaDI 原研究产物保持不变，新结果全部写入新目录。

先按 [MaDI 复现说明](MADI_REPRODUCTION.md) 准备原有 `dataset` 与 `study/retriever`。安装更新的 requirements.lock；新增依赖为 pycountry 24.6.1 和 cleanco 2.3。无需 GPU 或下载 RoBERTa 即可执行本轮。

从 [NIST FEIII 官方入口](https://ir.nist.gov/feiii/2016-challenge.html) 下载 2016 数据压缩包，保存到本地研究目录。固定 SHA256 为 `13e0a099ddaa32950f27267d5a7327f1a4c00a72585400cd0c8b1061db899413`，47 MB 左右；适配器会拒绝不同快照。原始数据不随本仓库分发。

在仓库根目录，使用已安装本项目的 Python：

```bash
python scripts/run_company_optimization.py prepare --dataset artifacts/research_20260915/dataset --previous-study artifacts/research_20260915/study --output artifacts/company_optimization_reproduction
python scripts/run_company_optimization.py evaluate --dataset artifacts/research_20260915/dataset --output artifacts/company_optimization_reproduction --feiii-archive artifacts/optimization_20260915/feiii-data-2016-final.zip
```

`prepare` 拒绝覆盖目录，只读取训练/验证特征，保存源代码归档、模型、校准、验证策略、数据哈希与 `frozen_config.json`。`evaluate` 先验证冻结输入，再执行 MaDI 复测和 FEIII 零样本迁移。FEIII 的明确 TP/TN 才进入混淆矩阵；Ambiguous、未裁决及正组件矛盾分别保留/隔离。

首次实跑在读取 LEI CSV 时中断：该文件既非有效 UTF-8，也不能完整按 Windows-1252 解码。修复后从**同一已校验官方 ZIP 内的 LEI.xlsx**读取 Unicode，未丢弃字符；FFIEC/SEC 仍为 UTF-8 CSV。原失败痕迹和 MaDI 复测保留，`resume-external` 只恢复这一读取步骤，检查旧模型/阈值未改并保存修复来源哈希。新复现使用当前读取器时直接 `evaluate`，不需要恢复步骤。此修复没有追加训练或外部评分后选模。

候选国家规范化可通过 `/match-runs` 或后台任务设置：

```json
{"method":"exact","candidate_mode":"fixed_iso","threshold":0.9,"review_threshold":0.5}
```

冻结公司特征模型通过服务和 Worker 的 `--model` 指向本轮 `company_lr` 或 `company_gb` 目录；选择 `method=company`。若使用 `hybrid_iso`，服务和 Worker 都传入相同 `--candidate-model`，指向冻结 retriever。后台任务也可将 settings JSON 文件传给 CLI `submit-job --input ...`。服务与执行者必须使用同一包版本和模型。

这些新路径只生成复核候选版本。新评分器未胜出，配置它仅用于对照/人工验证，不能把研究概率解释为实际业务正确率。MaDI 旧测试已见，FEIII 是历史金融企业裁决样本，均不替代独立业务人工验收。

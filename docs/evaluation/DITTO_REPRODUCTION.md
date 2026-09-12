# Ditto 训练、评测与公司服务接入

本项目使用 [Ditto 上游实现](https://github.com/megagonlabs/ditto) 的模型核心和删除增强，并适配冻结产物与现有审核流程。[设计与边界](../decisions/014-ditto-neural-matching.md)解释了上游归属、字段和阈值规则。

## 安装与权重

在独立 Python 3.12 环境中先安装核心锁文件，再选择 PyTorch 的官方 CPU 或 CUDA 构建。例如 NVIDIA CUDA 12.8：

```powershell
python -m pip install -r requirements.lock
python -m pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r requirements-neural.lock
python -m pip install --no-deps -e .
python -m pip check
```

CPU 环境将 PyTorch index 换为 `https://download.pytorch.org/whl/cpu`。普通使用者也可安装 `.[neural]`；锁文件用于复现本次依赖组合。本地测量使用项目独立的神经环境，核心依赖以只读路径引用现有锁定环境；正式复现采用上述完整安装，不依赖其他项目。

从 [FacebookAI/roberta-base](https://huggingface.co/FacebookAI/roberta-base) 获取 revision `e2da8e2f811d1448a5b465c236feacd80ffbac7b` 的 config.json、tokenizer_config.json、tokenizer.json、vocab.json、merges.txt 与 model.safetensors，放入 `artifacts/models/roberta-base`。下载示例：

```python
from huggingface_hub import snapshot_download
snapshot_download(
    "FacebookAI/roberta-base",
    revision="e2da8e2f811d1448a5b465c236feacd80ffbac7b",
    local_dir="artifacts/models/roberta-base",
    allow_patterns=["config.json", "tokenizer_config.json", "tokenizer.json",
                    "vocab.json", "merges.txt", "model.safetensors"],
)
```

权重 SHA256 必须为 `5bde1d28afb363d0103324efeb5afc8b2b397fe5e04beabb9b1ef355255ade81`，与 [官方文件页面](https://huggingface.co/FacebookAI/roberta-base/blob/main/model.safetensors)一致。本次经 HTTPS 镜像取得文件后以该官方值核对；训练和服务加载均在离线模式下工作。基础模型卡声明 MIT；原始权重与数据没有打进 EntityBridge 的 wheel。

## WDC 真实复测

按 [WDC 数据与划分记录](WDC_UNSEEN_RESULTS.md)取得归档，或使用项目现有 `artifacts/raw/wdc_products`。归档 SHA256 由适配器强制核查。

```powershell
$env:HF_HUB_OFFLINE="1"
$env:TRANSFORMERS_OFFLINE="1"
$env:TOKENIZERS_PARALLELISM="false"
python scripts/run_ditto_benchmark.py --device cuda --output artifacts/reports/ditto_reproduced
```

默认方案：seed 20260913、最多 20 轮、验证 F1 连续 4 轮不改善早停、长度 256、微批次 8、梯度累积 8（有效批次 64）、学习率 5e-5、weight decay 0.01、线性调度及 6% warmup、MixDA alpha 0.8、上游 del 增强、梯度检查点、GPU bf16 训练和 float32 推理。末尾不足一个批次的损失按真实样本数累计。它是本地预算方案，不能当成论文 50 轮训练的精确复现。

脚本默认仅在训练集重拟合固定 C=1 的旧基线。若保存了原来 WDC 实验的模型目录，可加 `--baseline-directory artifacts/reports/wdc_unseen_v1` 复用其冻结权重。本次比较采用后者，历史报告不覆盖。两种方式都对每个方法重新计算同口径的验证 F1/成本阈值。

输出包含 plan、源代码归档、数据隔离审计、训练 history、冻结模型、frozen_config、逐对本地预测和汇总 report。全程标记复测，不声称新封存测试，也不根据测试成绩挑选重跑。模型约 500 MB，数据和逐对预测留在本地，公开材料只包含经过检查的汇总证据。

## 公司人工审核数据训练

```powershell
entitybridge export-labels --output artifacts/review-labels
entitybridge train-review-ditto --input artifacts/review-labels --base-model artifacts/models/roberta-base --device cuda --output artifacts/company-ditto
```

导出仅纳入当前已发布且有效的 accept/reject，排除 abstain、撤销和过期判断。train/validation/test 每份至少 10 对、每类 2 对、2 个独立组；这些是执行门槛，不是足以验证生产模型的样本量保证。尚无足够真实公司审核标签时，可以完成软件链路测试，但不能宣称公司模型质量提升。

显式配置公司模型后，服务与独立 Worker 使用相同路径：

```powershell
entitybridge serve --model artifacts/company-ditto/model
entitybridge worker --model artifacts/company-ditto/model
```

沿用现有数据库、认证和工作区配置，在任务页选择 Ditto，或向 `/jobs` 提交 `settings.method="ditto"`。推理目前使用 CPU。结果先成为复核候选；人工接受及显式发布仍是必要的业务步骤。商品模型不能用于这条公司服务路径。

## 离线工程测试

```powershell
python -m pytest -q tests/test_ditto_contracts.py tests/test_ditto_benchmark.py tests/test_ditto_neural.py
```

这些测试训练随机初始化的微型 RoBERTa，只证明代码与事务链路；真实算法效果必须引用单独的 WDC 报告。

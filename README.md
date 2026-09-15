# EntityBridge

**企业实体关联与可撤销身份治理工作台。** 将不同来源的企业记录关联成统一身份，并保留原始记录、匹配依据、人工判断和历史去向。

同一家公司在两个系统中可能有不同名称；同一地址上的两家公司也可能是不同法人。EntityBridge 处理这类匹配，以及匹配之后的纠错：**为什么合并、谁确认过、撤销影响哪些记录、旧企业 ID 现在去了哪里。**

项目采用 Python、Splink、DuckDB/Parquet、SQLAlchemy、PostgreSQL、FastAPI 和 Jinja2，面向数据工程、数据质量与 Python 后端的工程展示。它是可运行的单机研究原型，尚未接入真实采购或 CRM 业务。

**开发版 0.6.0.dev0：** 人工判断现在有持久回执、客户端幂等键和显式失败恢复，候选准备后仍需单独发布。新增[六条企业记录操作卡](examples/company_workflow/OPERATION_CARD.md)，可自动走通导入、复核失败恢复、冲突拒绝、撤销和历史追溯。样例明确为合成数据；业务侧[公司复核评测](docs/evaluation/COMPANY_REVIEW_EVALUATION.md)仍等待独立人工标签。开发版不等于已发布版本。[恢复契约与兼容变化](docs/decisions/015-review-receipts.md) · [本地升级与实验验证](docs/evaluation/LOCAL_V06_VALIDATION.md)

**后续算法优化与外部验证：** ISO 国家名称规范化将 MaDI 复测的已知候选召回从 **57.39% 提高至 90.34%**，候选 2,052→3,121；新增参考 ING EMM 的公司特征模型和独立校准/策略划分。四模型对照未支持替换原实验基线，NIST FEIII 的 2,207 对专家二元裁决暴露跨域误匹配，保留人工复核。397 项全量回归和新安装包双后端工作流通过。[完整收益与负面结果](docs/evaluation/COMPANY_OPTIMIZATION_RESULTS.md) · [复现与使用](docs/evaluation/COMPANY_OPTIMIZATION_REPRODUCTION.md) · [实现设计](docs/decisions/016-company-country-features.md)

**上一轮公开公司研究：** 已完成MaDI-Bench Companies数据审计、保护已知实体的划分、四候选轨道×七评分器对照、三个Ditto种子和成对置信区间，保留596对测试标签。放宽检索国家限制使已知正例召回57.39%→100%，候选2,052→9,033；但Ditto前100对已知正例均值89，低于逻辑回归96，未证明神经模型增益，不替换默认策略。最终380项回归通过。[真实结果与代价](docs/evaluation/MADI_COMPANIES_RESULTS.md) · [公开数据与复现](docs/evaluation/MADI_REPRODUCTION.md)

[快速运行](#快速运行) · [实际结果](#实际结果) · [公开基准](#公开基准与评测协议) · [架构](#逻辑结构) · [复现](#测试与实验复现) · [成熟度与岗位定位](docs/review/MATURITY_AND_POSITIONING.md) · [v0.4 验证记录](docs/evaluation/V04_VALIDATION.md)

**v0.5 接入 Ditto / RoBERTa 神经匹配：** 采用上游 CLS、MixDA 与删除增强，保留 Apache-2.0 来源和许可证；提供验证集选模、双阈值对比、冻结 safetensors 产物及公司审核数据训练入口。WDC 商品配对复测中，同口径 F1 从商品逻辑回归的 **34.36% 提高至 61.96%**；但高误合并成本规则下仍更差，保留人工复核。商品结果不外推为公司质量。[实际结果](docs/evaluation/DITTO_RESULTS.md) · [验证](docs/evaluation/V05_VALIDATION.md) · [设计](docs/decisions/014-ditto-neural-matching.md) · [安装与复现](docs/evaluation/DITTO_REPRODUCTION.md)

**v0.4 增加了任务执行、复核学习与运维验证：**

- **持久后台任务**：提交、幂等、租约、取消、重试与崩溃恢复；候选版本和任务成功原子提交，正式发布单独进行。[ADR 009](docs/decisions/009-durable-jobs.md)
- **可审计复核学习**：导出有效人工判断，按依赖组件划分训练/验证/校准/测试，冻结候选模型；比较随机和主动采样。固定预算模拟未发现 F1 提升，不自动部署模型。[ADR 010](docs/decisions/010-review-learning.md)
- **备份、监控和真实 HTTP 实测**：双后端逻辑备份/恢复验证、受限指标与就绪接口，10k 合成记录的实际网络负载点。[ADR 011](docs/decisions/011-operations.md)
- **身份与工作区边界**：验证已签发的 RS256 JWT 与工作区角色，独立数据库/产物/进程配置；保留本地静态令牌方式。[身份验证](docs/decisions/012-identity-boundary.md) · [隔离工作区](docs/decisions/013-workspace-isolation.md)
- **更难的外部评测**：WDC 商品未见实体诊断，先审计并修正训练/验证重叠，再冻结四组方法进行测试；v0.4 四组基线在成本阈值下召回偏低，增加字段未改善声明的错误成本。[历史结果与限制](docs/evaluation/WDC_UNSEEN_RESULTS.md)

v0.3 的餐馆/论文基准、成本与概率诊断、独立关系查询投影继续保留。[历史验证记录](docs/evaluation/V03_VALIDATION.md)

![企业目录与来源追溯](docs/demo/08-current-directory.png)

## 已实现的工作流

1. **导入与保留版本**：对来源数据做字段校验和规范化，保留不可变记录版本；重复导入与来源更新有明确语义。
2. **生成候选与证据**：可信登记号匹配和无标识文本匹配分开；后者比较精确名称、模糊名称与冻结的 Splink 模型。
3. **约束归并**：独立 resolver 综合模型边与人工同一/不同判断；“不同企业”约束作用于整个簇，不能通过第三条记录绕过。
4. **复核与撤销**：人工确认、拒绝或暂缓；撤销前预览影响，保留理由和依据版本。撤销一项支持不保证一定拆簇。
5. **发布与追溯**：候选身份版本完整落盘后才切换当前指针；过期操作拒绝提交。旧版本可查询，旧 ID 展示合并或拆分后的全部去向。
6. **更新与重算**：固定候选策略下，在旧、新支持图的受影响闭包内重算；超过预算或策略变化时按相同语义全量兜底。

界面包含企业目录、关系复核、版本历史和管理员后台任务，目录与复核按每页 100 条展示，支持翻页及按复核状态筛选，翻页链接固定所依据的版本。目录和实体详情通过关系查询投影读取；复核与固定版本导出仍使用完整产物。分支机构、继承关系保留为来源关系，不直接推出同一法人。数据库初始化运行版本迁移；SQLite 用于快速演示，PostgreSQL 用于持久化与专项验证。

## 快速运行

需要 **Python 3.12**。在仓库根目录执行；无需下载真实数据、配置账号或运行 PostgreSQL。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m entitybridge.cli demo
```

Linux/macOS 对应命令：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e . --no-deps
.venv/bin/python -m entitybridge.cli demo
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，按 `Ctrl+C` 停止。端口占用时加 `--port 8001`。默认状态保存在 `artifacts/demo.sqlite`，重新启动保留操作历史；若设置了 `DATABASE_URL`，程序使用该数据库。

演示中的公司、地址与初始评分均为**合成治理案例**，用于展示错误合并、人工纠正和撤销；页面中的手设分数不是模型实测成绩。`demo` 只监听本机并授予演示管理权限。真实数据评测与合成演示分开，见下文。

已有环境时只需最后一条启动命令。实测基准环境为 Windows、Python 3.12.13、Splink 4.0.17、DuckDB 1.5.5；依赖版本见 [requirements.lock](requirements.lock)。CI 定义包含 Windows/Linux，实际执行状态以仓库 Actions 为准，不将配置文件等同于跨平台验证结果。

[五分钟字幕导览](docs/demo/EntityBridge-5min.mp4) · [演示操作脚本](docs/demo/WALKTHROUGH.md)。视频由实际浏览器截图和字幕组成，无音轨，不是一镜到底录屏。

后台任务、复核学习、备份与身份配置见 [v0.4 操作入口](docs/demo/OPERATIONS_V04.md)。

## 实际结果

主实验来自 **GLEIF Golden Copy 与 Companies House 官方数据**。最终固定发布集有 **94,104 条来源记录**；训练 59,764 条、验证 20,000 条、测试 14,340 条，对应测试企业 7,170 个。阈值只由验证集选定。原先看过的开发测试企业全部排除，最终 test 使用同一快照中剩余的企业，不是时间外推。

| 方法 | 测试 Precision | Recall | F1 |
|---|---:|---:|---:|
| 精确名称 | 1.0000 | 0.9905 | **0.9952** |
| 模糊名称，验证阈值为 1.0 | 1.0000 | 0.9905 | 0.9952 |
| 当前 Splink 配置 | 1.0000 | 0.8866 | **0.9399** |

**当前 Splink 配置弱于精确名称基线。** 两个公开来源的现名称大多一致，且可能依赖同一登记机关。这里观测到零误配不等于真实业务零风险；不能据此声称算法领先或跨国泛化。

主实验候选召回为 99.80%，全量产生 846,212 对候选；单次离线实验约 46.46 秒、进程峰值内存约 3.96 GB。另完成真实 10k 记录的 PostgreSQL/API 整合验收。离线计算规模、数据库整合与线上吞吐是不同测量，本项目没有生产 SLA。[完整结果与口径](docs/evaluation/RESULTS.md) · [主实验数值证据](docs/evaluation/evidence/release_final_v2_report.json)

v0.3.0 再次完成真实 10k 记录的 PostgreSQL/API 整合验收：7 项完整性检查通过，临时 schema 已清理。该流程的约 17.88 秒涵盖导入、匹配、准备、发布、查询和导出，不是网络吞吐测试；查询优化另有同版本、同参数的新旧路径成对测量。[本轮整合报告](docs/evaluation/evidence/public_benchmarks_v03/real_workflow_10k_v03.json) · [查询投影与测量](docs/decisions/008-query-projection.md)。[v0.2 历史整合报告](docs/evaluation/evidence/real_workflow_10k_v02.json) 保留原值。

### 曾用名的候选召回探索

只有曾用名且缺少地址时，原候选覆盖很弱。新增可选的冻结字符 n-gram TF-IDF 检索，在一个**已看过的探索性测试集**上，将候选召回从 **19.51% 提高到 46.06%**，同时候选对由 539 增至 18,481。它找到了更多可能配对，但自动模糊匹配 F1 从 **30.61% 降至 19.62%**，当前 Splink 仍未接纳配对。

这项增强用于复核候选探索，不能当成自动匹配质量提升，也没有替换默认匹配策略。服务中的 hybrid 模式不自动归并评分边，必须经人工明确接受；真实评分仍保留为复核证据。全局 top-k 的邻居可能因其他记录变化而改变，因此每次必须重新生成全量候选，不沿用固定桶的局部重算承诺。[匹配审查与复现](docs/review/MATCHING_AUDIT.md) · [探索性数值证据](docs/evaluation/evidence/hybrid_name_v2_summary.json)

### 公开基准与评测协议

新增两个作者公开提供的小规模配对基准：Fodors–Zagats 餐馆数据和 DBLP–ACM 论文数据。它们用于检验不同领域、字段缺失和标签覆盖条件下的行为；DBLP–ACM 仅将标题映射为名称，不能把结果当成企业身份识别能力，或与论文的完整字段成绩直接排名。

同一组记录分别运行固定候选、混合候选和仅给定标注配对三条轨道，比较精确名、模糊名、Splink 与训练集监督逻辑回归。未知配对单独计数；候选召回、端到端与条件指标分开报告。阈值只由验证集选定，分组 bootstrap 保留共享记录间的依赖；Brier/ECE 只诊断样本中的概率分数，不宣称部署校准。[数据来源与划分](docs/data/PUBLIC_BENCHMARKS.md) · [评测协议](docs/evaluation/EVALUATION_PROTOCOL.md) · [公开基准结果](docs/evaluation/PUBLIC_BENCHMARK_RESULTS.md)

当前 v2 报告明确标为 **`exploratory-replayed`**：v1 暴露了精确名称基线可被阈值 0 改成“接纳非精确名称”的语义问题，修复后在同一测试数据上重放。它不是新封存测试，也不是原论文的随机配对划分。该划分的 Fodors–Zagats 测试仅有 **55 个已标注配对、20 个正例**；即使某项满分，也不足以推断普遍准确率。新增逻辑回归 **仅供 benchmark 实验**，没有接入默认服务自动合并。

## 逻辑结构

```mermaid
flowchart LR
  S[来源快照] --> R[不可变来源版本]
  R --> V[八列无标识投影]
  V --> B[候选与冻结模型评分]
  B --> C[确定性约束解析]
  H[人工判断 / 撤销事件] --> C
  C --> P[完整身份版本]
  P --> T[事务发布指针]
  P --> Q[可重建关系查询投影]
  T --> Q
  Q --> U[目录 / 实体详情]
  T --> HUI[复核 / 历史 / 固定版本导出]
  R --> G[独立评估真值]
  B --> E[隔离效果评估]
  G --> E
```

模型给出证据，resolver 决定分区，存储层维护历史与当前可见版本。可信登记号入口 `POST /registry-runs` 独立于 `POST /match-runs`：编号、LEI、URL、真实企业分组和嵌入文本的标识不能进入无标识匹配器。模型与训练 TF 保存后冻结；来源版本或策略变化必须显式进入运行依据。

| 模块 | 职责 |
|---|---|
| `ingestion.py`、`normalization.py` | 来源解析、版本契约、无标识特征投影 |
| `candidates.py`、`matching.py`、`evaluation.py` | 候选、第三方模型/基线、登记数据效果与完整性评估 |
| `benchmarks.py`、`benchmark_metrics.py`、`supervised.py` | 公开部分标签适配、评测协议、仅用于实验的监督基线 |
| `ditto.py`、`ditto_training.py`、`ditto_learning.py` | 冻结神经模型、验证集选模与公司人工标签训练 |
| `resolution.py`、`identity.py`、`incremental.py` | 簇级约束、身份去向、受影响图重算 |
| `store.py`、`schema.py`、`database.py`、`query_projection.py` | 来源版本、判断事件、迁移、事务发布与可重建查询投影 |
| `api.py`、`web/` | HTTP 接口、权限和三页界面 |

项目实现的是这些模块之间的数据与治理契约；Splink 的概率关联/EM、DuckDB 的查询执行和第三方相似度算法不属于自研算法。[匹配设计](docs/decisions/001-matching.md) · [评测设计](docs/decisions/002-evaluation.md) · [身份](docs/decisions/004-identity.md) · [撤销](docs/decisions/005-revocation.md) · [增量](docs/decisions/006-incremental.md)

查询投影与候选版本在同一事务中写入，发布前与冻结产物逐项校验。旧版本首次查询需要一次完整校验和回填，随后目录分页与详情读关系表。名称仍采用字面子串匹配，会扫描所选版本的精简搜索值；没有把 `%term%` 包装成对数复杂度索引检索。深分页、长历史去向、复核和导出也各有剩余成本。[ADR 008：查询投影、迁移与性能边界](docs/decisions/008-query-projection.md)

## 测试与实验复现

常规测试使用合成夹具和临时数据库，不下载数据，也不要求 PostgreSQL：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m "not postgres and not network"
.\.venv\Scripts\python.exe -m ruff check src scripts tests migrations
.\.venv\Scripts\python.exe scripts\check_compatibility.py
```

测试覆盖来源更新、约束冲突、撤销、旧 ID、过期/并发操作、不可变模型、增量/全量等价、独立图参考和进程终止恢复。本轮审查还修复了历史数据重导入、撤销并发、空文本列评分及外部候选无法被评分等问题；这不代表不存在其他 bug。[存储审查](docs/review/STORE_AUDIT.md) · [匹配审查](docs/review/MATCHING_AUDIT.md) · [安全与接口边界](docs/review/SECURITY_AND_API.md)

真实数据流程使用新的输出目录，保留旧快照和结果：

```powershell
.\.venv\Scripts\python.exe scripts\fetch_public_data.py --raw-dir artifacts/raw/reproduced --ch-date 2026-09-01 --ch-parts 7 --gleif-pages 0 --golden-copy
.\.venv\Scripts\python.exe scripts\build_benchmark.py --raw-dir artifacts/raw/reproduced --output-dir artifacts/datasets/reproduced --sizes 10000 100000
.\.venv\Scripts\python.exe scripts\run_experiment.py --dataset artifacts/datasets/reproduced/real_10000 --output artifacts/reports/reproduced_10k
```

完整来源下载约 1 GB，还需为数据、模型和实验产物预留空间；下载脚本要求至少 4 GiB 可用。Golden Copy 使用下载时的最新发布，CH 历史文件也可能轮换。新日期的数据是新实验；要重放原实验输入，需要相同快照及 manifest 哈希，不能保证未来下载逐字节相同。

构建器按实体切分，matcher 只接收八列允许字段；实验在训练前验证文件哈希、实体互斥和记录覆盖。输出包含冻结配置、模型/TF、验证曲线、候选及端到端指标、错误分解、簇指标与资源用量。新执行结果应写入新目录，不覆盖封存证据。

主实验 94,104 条发布集与曾用名条件的构造步骤见 [重新封存说明](docs/data/V4_RELEASE_HOLDOUT.md)、[数据契约](docs/data/SOURCES_AND_CONTRACT.md)和[审计结果](docs/data/data_audit.md)。原始数据、真值记录与大型模型产物不随仓库发布；公开数值见 [证据 SHA-256 清单](docs/evaluation/evidence/SHA256.json)。

### 复现小规模公开基准

以下命令下载作者站点上已固定 SHA-256 的小文件，并写入新的转换/报告目录。输出目录不能覆盖；重复运行请换目录名。`--test-status` 必填，重放当前测试数据应使用 `exploratory-replayed`。

| 步骤 | 仓库根目录中的 PowerShell 命令 |
| --- | --- |
| 下载、校验与重新划分两个基准 | `.\.venv\Scripts\python.exe scripts\fetch_benchmarks.py --output-root artifacts/benchmarks/reproduced` |
| Fodors–Zagats 评测 | `.\.venv\Scripts\python.exe scripts\run_public_benchmark.py --dataset artifacts/benchmarks/reproduced/fodors_zagats --output artifacts/reports/reproduced_fodors --test-status exploratory-replayed` |
| DBLP–ACM 评测 | `.\.venv\Scripts\python.exe scripts\run_public_benchmark.py --dataset artifacts/benchmarks/reproduced/dblp_acm --output artifacts/reports/reproduced_dblp --test-status exploratory-replayed` |

默认误配/漏配代价为 10/1、bootstrap 为 400 次，这些是实验设定，不能代替业务代价调查。原始数据和转换记录仅保存在忽略的 `artifacts/` 中；数据页未明确独立数据许可，DeepMatcher 代码的 BSD 许可不能替第三方数据授权。当前封存结果来自 `public_fodors_zagats_v2`、`public_dblp_acm_v2`，见[结果与重放说明](docs/evaluation/PUBLIC_BENCHMARK_RESULTS.md)。

### 可选 PostgreSQL

已有 PostgreSQL 时设置 `DATABASE_URL`；[compose.yaml](compose.yaml) 提供数据库配置示例。`serve` 使用配置的管理、复核或只读令牌，配置格式见 [.env.example](.env.example)，命令行不会自动加载该文件。

Windows 也提供项目内 PostgreSQL 启停及隔离测试脚本；所需官方二进制、中文路径处理和专项命令见[数据库说明](docs/decisions/003-postgres.md)。默认合成测试与 PostgreSQL 专项分开，未启用的专项明确跳过。

## 适用范围与当前差距

项目的主要价值是**数据对齐后的可追溯纠错和可核验工程过程**，适合展示数据契约、评估隔离、事务边界与失败恢复。与成熟方案的官方能力比较、岗位定位和可核验描述见[成熟度审查](docs/review/MATURITY_AND_POSITIONING.md)。

- 主公司实验是能通过登记号交叉验证的闭集，两源可能同源；新增餐馆、论文基准仍是跨领域诊断，没有真实双人标注、跨时间业务验证或人工节省工时实验。
- 模型概率未校准；曾用名且缺地址时信息不足。扩大召回会增加误候选，需要独立标注和更可靠的评分策略。
- 目录与详情稳定查询已移至关系投影；名称子串仍筛查精简索引值，复核/导出仍读完整产物，固定策略增量仍扫描记录找桶。没有分布式执行、生产容量或长期稳定性承诺。
- 权限是**本机、单工作空间的 token 角色隔离**：viewer 读取已发布目录，reviewer 复核/撤销，admin 导入/评分/发布。它不包含企业 SSO、多租户隔离或完整互联网认证体系。
- 没有实际采购收益、生产部署或客户使用成绩；不会把候选召回改善表述为自动质量改善。

公开源码由严格白名单导出，排除内部规划、运行数据库、原始数据、凭据和旧工作区历史；`PUBLICATION_MANIFEST.json` 记录导出文件摘要。CI 检查合成测试、静态规则、发布边界及 wheel 中的 HTML/CSS 资源。

项目自身尚未选择发布许可证，没有代作者添加 `LICENSE`；第三方和数据条款见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。公开可阅读的源码不应被表述为已采用某项开源许可证。

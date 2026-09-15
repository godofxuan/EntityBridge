# Third-party software and data notices

记录日期：2026-09-12。本文件根据当前安装分发包的 metadata/许可证文件、[requirements.lock](requirements.lock) 和项目 [数据来源记录](docs/data/SOURCES_AND_CONTRACT.md) 编写。它列明项目所用组件与来源，不替换上游完整许可证，也不是对项目整体许可证的选择。

EntityBridge 自身尚未选择发布许可证，仓库没有添加项目 `LICENSE`。当前源代码交付不包含 `.venv/`、`.tools/`、原始数据、数据库或本地凭据；本地运行时由使用者取得这些依赖和数据。

## 核心软件

| 组件及本次版本 | 用途 | 分发包声明的许可证/说明 |
|---|---|---|
| Splink 4.0.17 | Fellegi–Sunter 概率关联、EM、比较层、TF 与 SQL 生成 | MIT |
| DuckDB 1.5.5 | 本地 SQL、匹配计算、Parquet 查询 | MIT |
| pandas 3.0.5 | 数据帧转换 | BSD-3-Clause |
| Apache Arrow / pyarrow 25.0.1 | Parquet 和列式数据 | Apache-2.0 |
| RapidFuzz 3.14.6 | 名称字符串基线 | MIT |
| scikit-learn 1.9.1 | 已锁定的可选模型分析依赖 | BSD-3-Clause |
| NumPy 2.5.3 | 数值依赖 | metadata 声明 BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0；保留分发包各附属许可 |
| SciPy 1.18.1 | 数值/科学计算依赖 | 主代码 BSD-3-Clause；wheel 内还含 OpenBLAS 等附属组件许可 |
| SQLAlchemy 2.0.52、Alembic 1.20.0 | 数据模型、事务、迁移 | MIT |
| psycopg / psycopg-binary 3.3.5 | PostgreSQL 驱动 | LGPL-3.0-only；二进制 wheel 的附属组件以其完整 notices 为准 |
| FastAPI 0.141.1、Pydantic 2.13.5 | API、请求校验 | MIT |
| Starlette 1.6.0、Uvicorn 0.52.4 | ASGI 服务 | BSD-3-Clause |
| Jinja2 3.1.6 | 服务端 HTML 模板 | BSD-3-Clause |
| python-multipart 0.0.32 | 表单处理 | Apache-2.0 |
| HTTPX 0.28.1、psutil 7.2.2 | HTTP 下载、进程资源测量 | BSD-3-Clause |
| SQLGlot 30.18.0 | Splink 的 SQL 处理依赖 | MIT |
| Altair 6.2.2 | Splink 的图表依赖 | BSD-3-Clause |
| igraph 1.0.0 | Splink 的图相关依赖，项目实际 resolver 独立实现 | metadata 标为 GPL；分发包附 GNU GPL Version 2 全文，使用范围以该包完整声明为准 |
| pytest 9.1.1、Ruff 0.16.7 | 测试、静态检查 | MIT |
| Hypothesis 6.168.0 | 属性测试 | MPL-2.0 |

上表不是所有传递依赖或 wheel 内嵌二进制组件的完整许可证清单。完整版本集合在锁文件中，安装后的原文位于各包的 `*.dist-info/licenses/`、`LICENSE*` 或组件目录。若另行打包、复制或分发依赖二进制，应保留对应包随附的完整许可和版权记录；不能把整个环境笼统标为 MIT。

可选 PostgreSQL 17.11 使用 [PostgreSQL 官方许可](https://www.postgresql.org/about/licence/)，Windows 二进制归档来自 [官方 Windows 下载入口](https://www.postgresql.org/download/windows/)链接的 EDB 发行。项目内归档的来源、字节数和 SHA256 保存在本地 `.tools/postgresql-manifest.json`；归档附属组件仍适用其各自许可。

## 可选神经匹配组件

[Ditto](https://github.com/megagonlabs/ditto) 的模型核心和数据增强代码在 revision `52985564a93fb11308439516d3e17a033d43ec8f` 上派生，许可证为 Apache-2.0。随本项目保留 [上游完整许可证](src/entitybridge/vendor/DITTO_LICENSE.md) 和 [原文件 SHA256/修改说明](docs/data/DITTO_UPSTREAM.json)，wheel 也包含许可证。修改涵盖正确的 padding attention mask、删除增强标签切片修复、本地安全格式权重边界及独立训练循环。算法来源归于 Ditto 作者，不称为 EntityBridge 自研。

可选运行依赖包括 PyTorch 2.9.1（BSD-3-Clause 及其随附第三方 notices）、Transformers 4.57.6（Apache-2.0）、tokenizers 0.22.2（Apache-2.0）和 safetensors 0.8.0（Apache-2.0）。[RoBERTa 基础模型卡](https://huggingface.co/FacebookAI/roberta-base)声明 MIT。本项目未分发 CUDA/PyTorch 二进制、基础权重或微调权重；完整传递依赖版本在 [神经依赖锁文件](requirements-neural.lock)。

## 公开数据

| 来源 | 本轮数据与条款 |
|---|---|
| GLEIF | LEI 和参考数据按其 [官方数据使用条款](https://www.gleif.org/en/meta/lei-data-terms-of-use) 使用 CC0。主实验从 [Golden Copy](https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy) 取得 2026-09-12 00:00 UTC 的完整 CSV，法定地址 GB 的记录按项目规则筛选。 |
| Companies House | 2026-09-01 月度 Basic Company Data，来自 [官方批量目录](https://download.companieshouse.gov.uk/en_output.html)。其 [版权与使用说明](https://www.gov.uk/government/publications/companies-house-accreditation-to-information-fair-traders-scheme/public-task-copyright-and-crown-copyright)区分自制 Crown copyright 材料、公开登记内容及第三方权利，不把所有 CH 数据称为 CC0。 |
| GLEIF 注册机关代码表 | 用 [官方 Registration Authorities List](https://www.gleif.org/en/lei-data/code-lists/gleif-registration-authorities-list) 核对 RA000585 的登记机关含义；原始表与下载证据留在本地数据目录。 |

每个原始快照保留 source_url、terms_url、发布时间、观测时间、字节数和 SHA256。源记录与派生特征保留出处；本项目不使用来源 logo，不声称获得 GLEIF、Companies House 或数据库/模型维护方背书。

公开注册地址可能涉及住宅。当前真实地址仅供本地研究，默认界面演示采用合成公司与合成地址；源代码交付不附原始大文件、人员信息或完整真实地址样本。再次取得不同日期的快照应生成新的清单，不能沿用旧数据的效果数字。

## 方法和项目贡献

2026-09-15 公司优化补充：使用 [pycountry 24.6.1](https://github.com/pycountry/pycountry)（PyPI 声明 LGPL-2.1-only，随包 ISO 数据以其附带许可为准）做标准字段精确映射，使用 [cleanco 2.3](https://github.com/psolin/cleanco)（MIT）处理公司法律后缀。两者通过依赖安装，不在项目中复制完整词表。公司特征设计参考 [ING EntityMatchingModel 固定 revision](https://github.com/ing-bank/EntityMatchingModel/tree/aa7c6e89f462d013c3f9fc308dd6d78a74620ae6)（MIT）；未复制其源代码、未安装完整 EMM，也未声称实现相同系统。包来源及 SHA256 见 [来源清单](docs/data/COMPANY_OPTIMIZATION_SOURCES.json)。

[NIST FEIII 2016](https://ir.nist.gov/feiii/2016-challenge.html) 的官方压缩包提供金融企业数据与专家裁决，本轮只在本地使用。其公开可下载性不等于本项目获得所有原始来源的再分发许可；不附原始公司表、工作簿、配对预测或训练权重。公开材料为方法、字段投影、文件哈希和汇总指标；原始来源及限制见[研究报告](docs/evaluation/COMPANY_OPTIMIZATION_RESULTS.md)。

Splink 的概率关联、EM、比较层和 TF 机制来自上游；DuckDB 查询引擎、RapidFuzz 相似度等也是第三方能力。EntityBridge 实现的部分是数据契约/防泄漏、固定规则候选组织、字段证据接线、独立约束决策、版本化身份、撤销抑制、事务发布、增量闭包和相应业务界面。

启动阶段还调研过 dedupe、ING EntityMatchingModel 和 UK DBT Matchbox，作为路线参考；它们没有作为本项目运行依赖安装，也没有因此取得这些组织的生产背书。来源记录保存在 原始调研说明（本地材料，未包含在公开导出中）。
# 演示视频构建工具补充

五分钟字幕导览使用从 PyPI `imageio-ffmpeg==0.6.0` Windows wheel 提取的 FFmpeg 7.1（Gyan build，启用 GPL/version3/libx264/libass）离线编码。wheel SHA-256 为 `02fa47c83703c37df6bfe4896aab339013f62bf02c5ebf2dce6da56af04ffc0a`。编码工具只保存在忽略的 `.tools/media`，没有加入应用运行依赖或随源代码分发其二进制。视频使用系统字体渲染字幕，未打包字体文件。

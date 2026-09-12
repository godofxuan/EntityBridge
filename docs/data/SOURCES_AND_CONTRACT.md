# 数据来源与无标识实验契约

核验日期：2026-09-12。所有下载留在本地 `artifacts/raw/`，原始大文件不提交仓库。

## 官方来源

1. [Companies House 官方批量目录](https://download.companieshouse.gov.uk/en_output.html)：2026-09-01 的七个 ZIP 分片。CSV 表头有前导空格，解析时只修剪表头；公司编号始终按字符串读取。来源目录说明该产品为登记册在册公司月度快照，不等同于所有公司均为 Active。
2. [GLEIF Golden Copy](https://www.gleif.org/en/lei-data/gleif-golden-copy/download-the-golden-copy)：该官方页实际加载脚本使用 `https://leidata-preview.gleif.org/api/v2/golden-copies/publishes`。已保存其 catalog 和 2026-09-12 00:00 UTC 的 LEI_3.1 CSV ZIP，逐行扫描并过滤法定地址 country=GB。主评测使用这一完整批量快照；API诊断页未混入。
3. [GLEIF 注册机关代码表](https://www.gleif.org/en/lei-data/code-lists/gleif-registration-authorities-list)：已下载 `2024-11-20_ra-list-v1.8.1.csv`。RA000585 行为 Companies Register / Companies House，地区具体为 England and Wales。主评测并非全英国各注册机关覆盖。

GLEIF LEI及参考数据按 [官方条款](https://www.gleif.org/en/meta/lei-data-terms-of-use) 使用 CC0；项目保留来源，不使用其商标或声称得到官方认可。

Companies House [版权与使用说明](https://www.gov.uk/government/publications/companies-house-accreditation-to-information-fair-traders-scheme/public-task-copyright-and-crown-copyright)区分自制的 Crown copyright 材料和公开登记内容。不能将所有 CH 数据标为 CC0；再使用时保留来源，不复制 Crown insignia，并注意第三方版权。公开演示使用合成地址，真实公司注册地址只供本地研究。

## 接入与快照

`scripts/fetch_public_data.py` 设置下载字节预算、剩余磁盘检查、有限重试、临时文件和 SHA-256。相同地址的已存文件须通过 manifest 哈希核验才能复用，发现无 manifest 或内容变化即报错。`scripts/build_benchmark.py` 在解析前再次验证原始文件哈希。

每个原始文件的 `.manifest.json` 保留 source_url、terms_url、downloaded_at、source_published_at、bytes、sha256、schema_version、parser_version。实际行数、隔离原因、字段缺失及筛选结果保存在数据审计 JSON。GLEIF 与 CH 快照相差11天，合法的名称或地址更新不能直接称为源数据错误。

ZIP 通过 `zipfile.open()` 流式读取，不解压文件路径。坏行保留在 evaluator 侧的审计目录。CH编号支持8位字母数字（包括 `IP10067R` 这类带后缀格式）；缺关键字段与列数错位隔离，不擅自移动错位字段。未知国家或来源空值保留 null，原文仍在 rich 层。

## Matcher 的唯一输入

每个数据集包含 `matcher/train.parquet`、`validation.parquet`、`test.parquet`，严格限定八列：

| 列 | 类型和含义 |
|---|---|
| record_id | 独立 UUID4，每条来源记录独立生成；不是编号哈希 |
| record_version_id | 独立 UUID4，相同来源内容复用，内容变化形成新版本 |
| source | `gleif` 或 `companies_house` |
| name | NFKC、大写、标点转空格；保留 LIMITED、BANK、GROUP 等词 |
| address | 法定/注册办公地址行的同种规范化视图；空值为 null |
| city | 同种规范化视图；空值为 null |
| postcode | 大写去空格；空值为 null |
| country | 来源提供的国家代码；CH已知英国地区映射GB，空值不补GB |

所有列为可空字符串。最终feature view为`identifier-missing-text-redaction-v2`：每条记录的注册号或LEI若嵌入上述允许文本字段，该字段置null；单纯删登记号列并不足够。`evaluator/truth_map.parquet` 仅包含 `record_id,true_entity_id,split`；`evaluator/rich_records.jsonl` 保留登记信息、原文、快照哈希和文件内定位。`private_registry.json` 保存不可推导的UUID映射，只供接入使用。正式本轮数据路径为`artifacts/datasets/v3/`，v1/v2保留作修正记录。

Matcher、候选器和resolver不得读取 evaluator、raw、private_registry 或有标识业务库。数据目录分离是代码契约；在单一开发用户下不是操作系统沙箱。部署独立匹配进程时只挂载 `matcher/`，不要把整个 artifacts 目录授予它。不能利用金标编号构造 cannot-link。

## 金标、抽样和划分

A类主评测保留 GLEIF `GB + RA000585 + GENERAL + ACTIVE + ISSUED`，登记号非空且格式合法；排除身份继承关联、一个编号多条GLEIF记录等歧义。CH相同登记号且状态严格为 `Active`，重复编号排除。两个来源的登记号关系建立evaluator真值，匹配器只能看八个允许字段。

从完整跨源重合范围按固定 seed=20260912 的实体哈希选5000/50000个企业，每个企业两条来源记录。再按独立固定实体哈希做60/20/20划分。一个企业的不同来源、版本和别名共享划分；每个split按随机opaque record_id排序，避免行邻接暗示答案。

这是编号完整且可交叉对应的闭集评测。两来源可能依赖同一登记机关，名称相同率较高，不能外推到真实供应商系统、跨国企业或全部缺编号企业。B类别名派生评测单独保存，明确只用了官方曾用名；复制的当前地址不是历史地址。C类合成治理案例不得混入A/B算法效果。

## 复现命令

```powershell
.venv/Scripts/python.exe scripts/fetch_public_data.py --ch-parts 7 --gleif-pages 0 --golden-copy
.venv/Scripts/python.exe scripts/build_benchmark.py --output-dir artifacts/datasets/reproduced --sizes 10000 100000
.venv/Scripts/python.exe scripts/build_benchmark.py --aliases-from artifacts/datasets/reproduced/real_100000/evaluator/rich_records.jsonl --output-dir artifacts/datasets/reproduced
.venv/Scripts/python.exe -m pytest tests/test_ingestion.py tests/test_normalization.py -q
```

复现一个已存在的封存输出目录会拒绝覆盖。新目录首次构建生成新的独立UUID；若要逐字节重放现有记录ID，需在接入端安全复用相应private_registry，而不把该文件交给matcher。相同seed的真实企业选择与划分不受随机UUID影响。

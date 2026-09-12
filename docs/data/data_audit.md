# 真实数据审计：2026-09-12

## 本轮结论

已下载并扫描 GLEIF 完整 Golden Copy 与 Companies House 七个官方分片。两源在保守筛选范围内共有 **57,170 个可交叉对应企业**，足够构建真实的10k/100k源记录实验。本报告只说明数据与接入事实；匹配精度、增量收益和人工效率需由后续实验分别证明。

正式无标识输入使用 `artifacts/datasets/v3/`。原始扫描审计为 `artifacts/datasets/v2/audit/data_audit.json`；v3从相同封存实体集合重新投影，增加文本中的标识清除，没有重新抽取企业。

| 实际执行项目 | 结果 |
|---|---:|
| 原始八个ZIP下载总字节 | 995,647,349 |
| GLEIF完整CSV扫描行数 | 3,429,068 |
| GLEIF法定地址country=GB | 231,640 |
| Companies House七分片原始行数 | 5,689,367 |
| CH成功解析记录 | 5,689,366 |
| CH结构错位隔离行 | 1 |
| 保守筛选后的唯一GLEIF登记号 | 57,852 |
| 跨源有效金标企业 | 57,170 |
| 一编号对应多个合格GLEIF记录的歧义编号 | 1，已排除 |
| 扫描、过滤、交叉核验耗时 | 129.044秒（不含下载和Parquet导出） |

硬件由主任务检查：12逻辑核、约33.95GB RAM。此扫描没有记录完整峰值内存序列，因此不报告未经测量的峰值。Python通过ZIP成员流式读取，未将约4.98GB的GLEIF CSV解压落盘。

GLEIF快照为2026-09-12 00:00 UTC；CH快照为2026-09-01。初始API观察的GB总数231,604属于前一份2026-09-11 16:00快照，不能与实际扫描的231,640混写。

## 真实筛选与错误修正

GLEIF主范围：`legalAddress.country=GB`、`registeredAt.id=RA000585`、`category=GENERAL`、`status=ACTIVE`、`registration.status=ISSUED`、合法非空编号；不纳入继承关联与同编号多LEI歧义。CH对应记录要求状态精确为Active。LAPSED只按当前实验范围排除，不解释为公司注销。

官方注册机关表将RA000585标为Companies House、England and Wales。真实GLEIF中该代码下也出现其他地区前缀；本轮按来源实际填写的机关代码和CH全局唯一编号交叉核验，不能把这个筛选结果描述为严格地理意义上的英格兰和威尔士全体企业。其他RA代码没有加入主金标。

扫描中已发现并修复两项不能悄悄掩盖的问题：

1. 初版编号正则只接受8数字或2字母+6数字，误排5,912个合法带字母后缀编号，例如`IP10067R`。修正为保留8位字母数字，实扫后主金标从57,160增加到57,170。v1数据和初始审计保留为历史，不能把这5,912条称为坏数据。
2. 仅删除登记号列仍不足以隔离答案：真实legalName或address中偶尔包含自身登记号/LEI。v3在允许的五个文本字段中检查这些标识，命中字段置null。100k集合实际处理6个name、8个address；10k集合无命中。三组曾用名派生集合各处理6个name。这些字段原文仍保留在evaluator rich层，匹配器不读取。

唯一真正隔离的CH行是`part6_7.csv:252222`：55列表头下出现额外字段，导致SIC到后续字段错位。原始文件未修改，也未凭猜测重排；完整坏行在`v2/audit/quarantine.jsonl`。

## 封存的实际档位

固定seed为20260912。A类从完整有效重合范围按实体哈希选择，各企业两条来源记录。B类从A类50k实体中选择具有至少一个不同官方PREVIOUS_LEGAL_NAME的9,298个企业，每个企业取按字符串排序的第一个曾用名构造一条GLEIF侧查询记录，配一条CH当前记录。

| v3目录 | 企业数 | 源/派生记录数 | train / validation / test记录数 |
|---|---:|---:|---|
| real_10000 | 5,000 | 10,000 | 6,042 / 1,938 / 2,020 |
| real_100000 | 50,000 | 100,000 | 59,764 / 20,000 / 20,236 |
| aliases_name_only | 9,298 | 18,596 | 11,196 / 3,596 / 3,804 |
| aliases_partial_address | 9,298 | 18,596 | 11,196 / 3,596 / 3,804 |
| aliases_current_address | 9,298 | 18,596 | 11,196 / 3,596 / 3,804 |

B类name_only移除查询记录全部地址字段；partial_address只保留查询邮编；current_address复制当前GLEIF地址，**并非历史地址**。这三组是官方名称派生实验，不是三个真实独立业务源。

A/B均按真实企业固定划分，没有跨split实体。每条源记录使用独立UUID4，文件按opaque ID排序，避免相邻两行泄漏配对。匹配列严格限定为record_id、record_version_id、source、name、address、city、postcode、country；truth_map、原文、编号和URL都在evaluator侧。

已完整验证五个v3数据集：25个清单文件的SHA-256、各Parquet列allowlist、全部实体的split唯一性，以及各记录允许文本中不含自身登记号/LEI。实际结果在`artifacts/datasets/v3/integrity_check.json`。这是数据生成器与文件隔离验证，不是操作系统层的访问控制证明。

## 难度与样本偏差

在完整57,170个金标企业中，两来源经标准化后名称不同606对、地址不同12,194对、邮编不同3,273对、城市不同5,536对。CH缺失country 12,480条、city 716条、postcode 85条、address 62条；缺失值保持null。这些都是配对样本范围内的计数，不是整个登记册字段质量估计。

名称一致占多数，且GLEIF可能与CH共享登记机关来源。A类总体成绩容易很好，不能外推为对全部无编号、跨国或真实供应商输入的效果。B类别名条件与真实难例需独立报告。未匹配上的682个合格GLEIF编号中包含CH状态被排除等情况；不能将所有未进入主集者当作负例。

## 可核验的真实例子

| 场景 | 官方记录证据 | 本轮解释 |
|---|---|---|
| 同企业名称变化 | GLEIF `254900IJ1O8JY3RBN753` 为MOTOHART (UK) LIMITED；CH `04776109`为[VIPER HELMETS LTD](https://find-and-update.company-information.service.gov.uk/company/04776109)，页面也含MOTOHART曾用名 | 登记号支持同一实体；不预先声称name-only已匹配成功 |
| 标准化同名、实际不同编号 | CH `05105933`为A & J WEALTH MANAGEMENT LIMITED，`03949662`为A.J. WEALTH MANAGEMENT LIMITED；均被标准化为A J WEALTH MANAGEMENT LIMITED | 名称标准化会产生真实碰撞，不能只凭名称union |
| 多企业共享地址 | 50k实体CH侧有一个相同地址+邮编桶含428个不同编号；其中[WARREN ADVISORS LIMITED](https://find-and-update.company-information.service.gov.uk/company/11197585)与[STENEO LTD](https://find-and-update.company-information.service.gov.uk/company/14858981)均经官方页面核验名称 | 同址不是同企业；报告不公开复制地址文本 |
| 证据不足、范围外 | API诊断记录`03EINY24LQ6IXW124R72`，THE BARCLAYS BANK UK RETIREMENT FUND - BARCLAYS BANK SECTION，RA999999且registeredAs缺失 | 不强行与CH记录连接，不造负例；不进入A类金标 |

这些是来源和规则核验案例，不是声称已经完成了独立双人业务标注。下游匹配结果需另行保存其评分与候选证据。

## 工程验证与后续边界

`tests/test_ingestion.py`、`tests/test_normalization.py`已运行14项关键行为验收：编号与空值保留、结构错位隔离、官方格式解析、明确金标范围、opaque ID重传复用、实体划分、压缩HTTP下载与哈希复用、原始快照篡改检测、冻结目录拒绝覆盖、别名派生标签和文本标识清除。

尚未在这里报告候选召回、模型收益、簇准确率、真实人类复核效率或生产运行承诺。后续实验只读取v3匹配视图，模型选择用train/validation，测试结果封存。

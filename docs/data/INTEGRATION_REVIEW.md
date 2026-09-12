# 独立集成审查（只读）

审查时间：2026-09-12。范围：`src/entitybridge/api.py`、`store.py`，并阅读相关schema、resolver和UI。未修改这些模块；用临时SQLite工作区、实际Store与FastAPI TestClient运行了下述复现。数据侧14项测试另已通过。本报告是修复前记录，修复后需补相应回归验收。

## P1：评分输入被更新后，旧分数可附着新记录发布

位置：`api.py:70`的`run_matching()`；`store.py:114`的`prepare_revision()`。

`run_matching`先读取active_records并评分，`prepare_revision`随后再次读取active_records。准备阶段没有验证每条评分边的left_version/right_version与该次源版本一致，也没有使用评分前冻结的input hash。发布时仅比较准备阶段新读到的input hash，因此无法发现评分早已过期。

已运行复现：两个来源原来都叫ALPHA LIMITED，name_exact得到score=1；在评分返回前更新右侧为OMEGA HOLDINGS、邮编也改变。随后prepare/publish成功，产物records是ALPHA/OMEGA，edges.evidence仍是ALPHA/ALPHA，且最终只有1个企业。实际评分边已经包含旧left_version/right_version，但Store忽略了它们。

修复建议：匹配任务携带冻结记录版本/输入哈希到prepare；准备时原子读取依据并拒绝输入变更；校验每条边端点版本与冻结记录一致。源更新也可能发生在decide/revoke提交事件之后与prepare之间，因此共用的prepare边版本校验同样必要。回归应在真实评分与prepare之间插入源更新，并断言返回版本冲突、旧发布版本不变。

## P1：策略变化使人工判定不同静默失效

位置：`store.py:203`的`_constraints()`，尤其`decision['policy_version'] != policy`筛选。

已运行复现：p1下人工reject两条记录并发布后为2个企业；只将policy_version换成p2，相同0.99模型边再次prepare/publish后变为1个企业。该人工决策的历史仍只有CREATE，没有EXPIRE/SUPERSEDE，UI仍把它当可撤销的有效判断。

这会在调阈值、重训模型或blocking改版时丢掉管理员已经确认的不同企业约束。源记录版本变化也采用静默continue，存在同样的状态表达问题。

修复建议：明确区分基于源记录的人工same/not-same和只针对某模型依据的自动抑制；前者应在端点版本未变时跨模型保留，或通过显式迁移/失效事件进入待处理状态。后者可以限定模型/策略范围。若源版本变更导致人工依据需失效，应形成可见事件并给出待复核原因。回归必须覆盖p1 reject→p2评分重跑，验证不会悄悄重新归并。

## P2：本地自动管理员缺少可信Host边界

位置：`api.py:98`的写入中间件与`api.py:114`的actor。

已运行TestClient请求：`local_demo=True`，Host为`attacker.example`且Origin为`http://attacker.example`，向`POST /imports`提交新记录，返回200并导入成功。Origin和Host相等并不证明该Host属于本地应用；socket为loopback时仍授予自动admin，这为浏览器DNS rebinding提供入口。

修复建议：默认只接受明确配置的localhost/127.0.0.1/[::1] Host（测试域可显式追加），先做可信Host校验，再做Origin校验；外部Host不应获得local_demo自动身份。现阶段不需要新增外网部署，只需守住本地运行边界。

## P2：确认撤销没有绑定用户看过的预览

位置：`api.py`的RevokeRequest和`/ui/revoke`，`store.py:292`的revoke。

预览响应包含event_cutoff，但确认请求只提交base_revision。revoke会重新调用revoke_preview，用刚生成的新cutoff自行比较。若另一位复核人在预览与确认之间写入人工决策但尚未发布，base_revision保持不变，用户确认的影响范围已经变化，服务器仍接受。

已运行三节点复现：AB和BC为0.99自动边，另有人工accept AB。撤销AB的已显示预览为2个企业；另一位复核人在相同发布版本下accept AC后，按原base_revision确认撤销成功生成1个企业。预览cutoff为1，生成候选cutoff为3，发布基线始终未变。

修复建议：预览返回并签名/持久化输入版本、event_cutoff和影响结果哈希，确认提交该token或至少预览cutoff；事务内若有变化就409并要求重新预览，不把内部新算的预览当作用户已确认的依据。

## 已看到的有效保障

- 数据库发布采用写锁+expected_parent与输入/事件截止序号校验。
- 产物先完整写入、fsync并重命名，数据库指针后发布；查询验证产物SHA-256。
- 历史读取使用已存不可变payload；旧ID可沿published lineage返回全部当前去向。
- 人工决策提交检查当前发布版本、端点记录版本和矛盾约束。
- API使用字段allowlist、请求体5MiB上限、写权限区分、Jinja转义及CSV公式前缀保护。

这些保障不能替代上述跨阶段输入与人工依据绑定。优先修复前两个P1，再补预览绑定与可信Host验收。

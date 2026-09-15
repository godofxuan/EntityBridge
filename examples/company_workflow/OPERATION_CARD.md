# 六条企业记录：从导入到纠错追溯

`sources.json` 是明确标注的合成样例：两个来源各三条记录。它用于验证治理流程，场景中的“同一/不同”是预设练习，不是人工标注真值或模型质量证明。

## 三步自动验收

在已安装核心依赖的源码根目录运行（PowerShell）：

```powershell
$env:PYTHONPATH=(Join-Path (Get-Location) 'src')
python scripts/check_company_workflow.py --output artifacts/company-workflow-first-run
Get-Content artifacts/company-workflow-first-run/report.json
```

输出目录必须是新的。脚本启动本机真实HTTP服务，使用临时数据库、临时鉴权令牌和独立CLI Worker，结束后清理进程与数据库，保留报告和脱敏HTTP操作记录。不会修改已有演示数据库。安装包验收时，把 PYTHONPATH 设为安装目标目录，并传同一路径给 `--installed-root`。

## 要观察的业务步骤

1. 重复导入 supplier_master/customer_master，记录身份保持；创建匹配任务，由独立Worker准备，管理员发布后目录才更新。
2. NORTHSTAR COMPONENTS 与 NORTHSTAR PARTS 是预设的别名案例；另一条同名但地址不同的记录预设为不同企业。复核时同时看原字段、候选规则和版本。
3. 先拒绝同名异企并发布，再接受别名关系。注入一次候选构建失败：判断已保存，有可查询回执；相同键重试不新增事件。显式恢复后准备候选，仍需管理员发布。
4. 尝试通过第三条记录绕开“不同企业”约束，应拒绝且不增加判断。
5. 更新独立记录的地址并重新匹配，检查未改变成员集合的实体ID保持。
6. 预览并撤销接受判断，携带预览前沿；检查旧发布parent拒绝、正确parent发布、旧ID全部去向及历史成员。
7. 验证viewer/无效令牌的访问边界，检查目录、回执、历史、固定版本导出，以及备份恢复后查询和回执一致。

这是自动HTTP验收。脚本不能替代第二位使用者验收、真人操作计时或真实采购/CRM接入。交互式既有演示仍使用 README 的 `entitybridge demo`，本操作卡样例不自动写入该数据库。

## 固定规模与PostgreSQL

传 `--capacity-size 100`、`1000` 或 `10000` 使用另一个固定生成器：双来源、每桶恰好一对、最大簇2；一轮预热和三轮实测，每轮30次固定版本目录查询。保留候选规模、原始延迟、Worker进程树采样RSS及机器配置。内存包含启动器和后代进程，共享页可能重复计数，不含独立API/数据库服务；它不是PSS或整机峰值。队列等待包含进程启动；未单独测锁等待。

`--postgres` 只允许显式 `_test` 数据库，创建和清理本轮专用schema。使用私有 `ENTITYBRIDGE_TEST_DATABASE_URL`，本地也可读取未公开的项目测试配置。不要把生产数据库用于该脚本。固定候选密度的单机测量不能推断高密度候选、大簇、并发吞吐或生产SLA。

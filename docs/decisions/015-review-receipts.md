# ADR 015：人工判断回执与显式恢复

状态：本地实现，0.6.0.dev0；数据库迁移 0006。发布状态以发布记录为准。

## 问题与边界

人工判断事件提交后，候选构建可能因文件或数据库故障失败。原入口会返回500，同一意图再次提交又生成事件。用户不能从响应判断“判断未保存”还是“已保存但构建失败”。回执把人的意图和派生构建的状态分开，复用既有 resolver 和 prepare_revision，不引入新的匹配任务队列。

## 提交、恢复、发布

- 判断/撤销事件与 `review_operation` 在同一个数据库事务保存。数据库级唯一幂等键防止重复意图。请求哈希包含操作类型和服务端认证的 reviewer；端点及版本按顺序规范化，理由去除首尾空白。
- HTTP 判断和撤销必须带 `Idempotency-Key`；UI 表单持有固定键。相同键和请求返回原回执，异参409。直接 Python Store 未传键时会产生新键，仅保留调用兼容，不具备客户端重试保障。
- 状态为 accepted → building → prepared/build_failed/stale_basis。每次构建取得默认300秒租约和新的 token；过期后显式恢复，旧执行者不能登记候选。没有心跳续租，长于租约的构建不保证成功。
- 冻结来源 hash、发布 parent、判断 event_seq、policy。构建读依据时及最终事务都验证。来源、发布或判断前沿变化时成为 stale_basis，保留事件供人检查；恢复不会悄悄采用新依据，也不会自动生成 REVOKE。
- 候选、查询投影与 prepared 回执在一个最终事务登记。文件可以先存在；失败留下的孤立文件不成为发布版本。既有产物检查负责识别孤立文件。
- prepared 不代表已发布。管理员仍须携带 expected_parent 单独发布。回执记录的是构建结果，不能从 prepared 推断当前发布指针。

## HTTP 和 UI 契约

`POST /reviews/decision`、`POST /decisions/{id}/revoke` 返回200（prepared）或202（已保存但尚未准备好），并给出 `Location`、operation_id、status_url、retry_url、decision_id、event_seq 和 safe_error_code。遇到数据库异常返回安全的503，状态可能未知；客户端保留原键重试，不能假设判断未保存。原始异常与凭据不回显。

`GET /review-operations/{id}` 查询，`POST /review-operations/{id}/retry` 显式恢复。列表默认100、最多200；没有完整回执分页，旧回执仍可按ID查询。reviewer/admin可查看本工作区回执，原reviewer或admin可恢复，viewer不可访问。工作区沿用独立数据库/产物/进程边界。

UI 的 `/review-operation/{id}` 展示“已保存”“候选失败”“等待租约”“依据过期”，历史页列出最近100条回执，判断及撤销后跳转结果页。来源更新引起的过期回执应先核对当前事实与历史事件，再决定业务操作。

`serve --disable-review-recovery` 关闭显式恢复入口，保留读取；该开关不关闭新的人工判断，也不终止已开始的构建。

## 迁移与恢复

0006只新增回执表；旧事件不补造操作键或回执。启动迁移器随 wheel 打包，Alembic 0001—0005保持不变。旧v0.5不能打开0006库。

逻辑备份验证回执、事件、冻结依据及候选关联。恢复 accepted/building 时清除租约并标记 build_failed/restore_interrupted，保留事件，需显式恢复。备份要求与当前schema相同；旧0005备份先用旧版恢复，再升级。代码回退使用升级前数据库与产物副本；不通过删回执表假装兼容。未验证生产PITR、RPO/RTO或无损保留升级后写入的回退。

验证覆盖：双后端幂等/并发、写文件/rename/最终提交边界、旧租约执行者、来源在产物落盘后变化、备份篡改、真实进程终止；源码外旧wheel升级和回退；真实HTTP与独立Worker。具体次数和结果以本轮完成回执为准。

# ADR 013 — 启动时选择隔离工作区

状态：2026-09-12 实现；v0.4。

每个工作区使用独立数据库、独立不可变产物目录、独立令牌配置或 OIDC 工作区角色。API 与 worker 进程在启动时读取相同配置并选择同一名称。HTTP 路径、查询参数、请求头和 JWT 都不能切换 Store。两个工作区各运行一个 API 与 worker；这不是共享表多租户 SaaS 或租户自助管理门户。

配置只保存数据库与静态令牌的环境变量名称，不保存秘密。参见 [示例](../data/workspaces.example.json)。SQLite 路径相对配置文件解析；所有工作区必须有不同数据库文件与不相交的产物目录。PostgreSQL 按声明的主机、端口和数据库名检查重复，凭据不同不能绕过重复检查；禁止 URL 中的 database/dbname 覆盖。OIDC 的 workspace 必须等于配置名称。

```text
entitybridge init-db --workspace-config workspaces.json --workspace finance
entitybridge serve --workspace-config workspaces.json --workspace finance --port 8001
entitybridge worker --workspace-config workspaces.json --workspace finance
```

配置包含多个工作区时，所有数据库环境变量均须存在，以便启动前进行整体重复检查。生产部署应为各进程分配各自的数据库账号、数据库授权和文件访问权限，独立容器或 OS 账号作为进一步边界。配置检查不能证明 DNS 别名没有指向同一服务器，不能替代数据库授权，不能隔离拥有同机全部文件权限的操作系统用户。

v0.4 的 `0005` 迁移增加单行 `workspace_binding` 表。选定工作区的 API/CLI 初始化后，先在数据库事务和工作区锁内确认归属：首次绑定名称，后续必须同名。经不同 URI、DNS 或主机别名连接同一物理库，也不能再绑定为另一个工作区。归属写入遵守数据库唯一和单行约束，两个名字同时首次启动只有一个能获准。备份恢复保留归属；没有隐式重命名工作区的入口。

PG 连接查询参数只允许显式 TLS 与 connect_timeout 项，阻止 host/hostaddr/port/service 等覆盖表面连接地址。静态令牌在同一配置的所有工作区之间也必须不同。此检查不枚举独立配置文件中的令牌，部署仍应配置最小权限、独立秘密和文件权限。

实际集成测试运行两个 SQLite 服务，验证来源记录、发布版本、企业查询、任务和静态令牌互不通用；另测试数据库复用、嵌套目录、OIDC scope 误配和 URL 覆盖被拒绝。JWT 跨工作区与角色验证见 [ADR 012](012-identity-boundary.md)。尚未进行独立主机/企业身份平台的部署认证。

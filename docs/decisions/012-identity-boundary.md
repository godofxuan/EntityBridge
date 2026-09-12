# ADR 012：单工作区的可选 OIDC JWT 资源服务器验证

状态：已实现；真实 RSA 签名与本地 JWKS HTTP 服务的 48 个验证用例通过。未连接企业身份提供方，未实现浏览器 OAuth 授权登录。

## 目标和边界

已有静态令牌把身份与角色保存在管理员配置中。新增 `entitybridge.auth` 接受受信任身份提供方签发的 JWT Bearer 令牌，将经过校验的 subject 和当前工作区角色交给现有 viewer/reviewer/admin 权限检查。

这是 **JWT resource server 验证接口**。应用不向身份提供方发起授权码请求，不实现 PKCE、回调、刷新令牌、用户开通、组织目录同步或统一登出。登录表单仍用于粘贴已经取得的 Bearer 令牌；不能把本次交付描述为“已接通企业 SSO”。实际接入仍需要管理员取得身份提供方配置、为 API 注册受众、配置角色 claim 并执行该提供方的验收。

## 受信任配置

```python
from entitybridge.auth import OidcConfig, OidcVerifier

verifier = OidcVerifier(OidcConfig(
    issuer="https://idp.example/realms/company",
    jwks_url="https://idp.example/realms/company/protocol/openid-connect/certs",
    audience="entitybridge-api",
    workspace="company-data",
))
subject, role = verifier.verify(bearer_token)
```

示例域名只是配置示意，不代表已经连接的服务。`issuer`、`jwks_url`、`audience`、`workspace` 必须来自管理员配置。请求参数、URL 路径、租户 header 和 token 中的 `jku`/`x5u`/`jwk` 都不能选择或覆盖它们。令牌 header 中的 `kid` 只在配置好的 JWKS 内选择密钥，不作为网络地址或文件路径。

生产配置要求 HTTPS；拒绝 URL 凭据、fragment、issuer query 和非法端口。`allow_loopback_http=True` 仅为本地测试保留，且只允许 `127.0.0.1`、`::1`、`localhost`，不是通用 HTTP 开关。默认选项为：网络操作 timeout 3 秒、JWKS cache 300 秒、未知 kid 刷新冷却 30 秒、时钟容差 0 秒。配置值必须是有限数字；timeout 上限 30 秒、cache 上限 3600 秒、冷却上限 300 秒、容差上限 60 秒。

每个服务实例只对应一个已配置工作区。claim 形状为：

```json
{
  "iss": "https://idp.example/realms/company",
  "aud": "entitybridge-api",
  "sub": "stable-user-id",
  "exp": 2000000000,
  "entitybridge_roles": {
    "company-data": "reviewer",
    "another-workspace": "admin"
  }
}
```

当前服务只取 `entitybridge_roles[configured_workspace]`，其值必须精确为 viewer、reviewer 或 admin。其他工作区的 admin、顶层 role、任意自带 workspace 值都不能提高当前权限。缺少当前工作区、未知角色、角色数组和错误 claim 类型全部拒绝。这提供单个实例的工作区授权边界，**没有把数据库改造成共享表的多租户隔离系统**。

## JWT 与密钥验证

固定使用 `RS256` 允许列表，不读取不受信任的 header 来决定允许哪些算法。使用 PyJWT 完成签名和标准 claim 校验，要求 exp/sub/iss/aud 存在；nbf、iat 存在时也检查时间有效性，并要求时间为有限 NumericDate。明确启用 RSA 最小密钥长度检查，测试中的 1024 位 RSA 被拒绝。subject 必须非空、不含控制字符且不超过 100 字符，以匹配当前审计 reviewer 字段；若身份提供方使用更长 subject，需要先迁移该字段及其接口约束。

JWT 长度限制为 16 KiB；kid 为非空字符串且不超过 200 字符。拒绝 `none`、HS256、RS512、未知 critical header 和不支持的 detached payload 形式。签名密钥必须是 RSA/RS256。issuer 精确匹配配置值；audience 必须包含配置的 API 受众。管理员应给 API 配置专用 audience，不能以面向浏览器客户端的 ID token 受众替代 API access token 配置。

实现使用精确固定的 `PyJWT[crypto]==2.14.0`，由 cryptography 提供 RSA 实现，没有自写 JWT 密码算法。PyJWT 文档要求算法允许列表来自应用配置，并支持标准 claim 验证选项。[PyJWT API 文档](https://pyjwt.readthedocs.io/en/latest/api.html)。本次核验了 [PyJWT 2.14.0 官方发布](https://pypi.org/project/PyJWT/2.14.0/)，仅新增其必要依赖，锁定为 PyJWT 2.14.0、cryptography 50.0.1、cffi 2.1.1、pycparser 3.0；未升级既有依赖。

## 缓存、轮换与故障处理

使用 PyJWT 的带 TTL 的 JWKS set 缓存与线程锁，不启用其无时间期限的 per-key LRU。正常重复验证使用有效缓存；缓存到期后必须重新获取。移除的密钥最多可能在当前缓存 TTL 内继续有效，到期后不再使用旧 key。未知 kid 在冷却允许时刷新一次；突发随机 kid 不能每次都触发刷新。默认 30 秒冷却也意味着刚轮换的新 kid 可能短暂被拒绝，身份提供方应在实际切换签名 key 前发布新公钥。[PyJWT JWK Client 文档](https://pyjwt.readthedocs.io/en/latest/api.html#jwt.PyJWKClient)。

PyJWT 2.14.0 的 JWKS 客户端禁止 HTTP 重定向；token 自带 jku 不被采用。网络 timeout、HTTP 错误、无效 JWKS、未知 kid 或验签失败全部变成相同的 `AuthenticationError("Invalid bearer token")`，不向响应携带 token、claims、密钥、URL 或底层异常。有效缓存中的已知 key 可以在暂时断网时继续验签；过期缓存、未知 key 和首次获取失败都会拒绝认证，不通过使用 stale key 绕过检查。

timeout 限制网络操作等待，不是整个 HTTP 请求的端到端时延 SLA；DNS、线程池排队、网络和身份提供方响应仍属于部署条件。没有为任意身份提供方的吞吐、巨大 JWKS 响应或持续网络故障给出生产容量承诺。

签名通过不等于实时撤销查询。没有 introspection、撤销列表或 back-channel logout；已签发令牌通常在 exp 前保持有效，密钥撤除还受上述缓存期限影响。应由身份提供方签发短有效期 API 令牌，配置必要的吊销策略；这些是外部部署条件，不能用本地模拟测试替代。

## 验证证据

`tests/test_oidc.py` 生成真实 RSA 密钥，通过真实 loopback HTTP JWKS 服务验证，共 48 项通过。测试覆盖：标准 claims 和强制 claims、错误 issuer/audience、跨工作区角色、三种合法角色、签名错误、none/HMAC/非允许算法、1024 位弱 key、未知 critical header、受限 token 长度、key 轮换/移除、未知 kid 冷却、并发首次获取、缓存有效和过期期间的网络故障、超时、错误 JWKS、禁止重定向、恶意 jku 无法更改端点，以及 HTTPS 与配置参数约束。

这是本地密码学与网络边界回归。它证明验证器按配置工作，不证明任何特定企业身份平台的账号、组织关系、令牌获取、注销或权限生命周期已经接通。

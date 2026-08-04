# ADR-012：采用 EdDSA 并规范 JWKS 轮换

状态：Accepted / Active  
批准日期：2026-05-20  
生效日期：2026-05-31  
Owner：Trust Foundations  
Supersedes：ADR-009

## 决策

Sentinel 使用 Ed25519 key 和 `alg=EdDSA` 签署 Atlas access token。access token 有效期固定为 15 分钟。
Gatehouse 缓存 JWKS 10 分钟；遇到未知 `kid` 可以提前刷新一次。

正常轮换顺序：

1. 在 JWKS 预发布新公钥，至少等待 10 分钟。
2. Sentinel 开始用新 `kid` 签发 token。
3. 旧公钥继续保留 30 分钟，最低重叠窗口不得小于 15 分钟。
4. 确认未知 `kid` 和 token validation error 没有异常后移除旧公钥。

紧急吊销可以缩短重叠，但必须由 Trust Foundations owner 与 Incident Commander 双人批准，并通知
Gatehouse、Relay 和 Foundry 管理入口的值班团队。

## 影响

- Sentinel：生成 Ed25519 key、签发 EdDSA token、发布 JWKS。
- Gatehouse：验证用户 token，支持 unknown-kid refresh。
- Relay：验证内部 delegated assertion。
- Foundry：验证短期 ingestion admin assertion。

Polaris、Constellation 和 Prism 不直接解析用户 token，因此不参与 JWKS 缓存。

## 验证

轮换演练必须覆盖新旧 token 并存、缓存尚未刷新、未知 `kid` 提前刷新和旧 key 到期。INC-2026-0527
暴露了旧 key 立即移除的问题，因此 Runbook 2.1 把 30 分钟 overlap 和监控门禁设为强制步骤。


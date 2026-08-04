# Sentinel 服务说明

状态：Active  
版本：2.4  
Owner：Trust Foundations  
值班：Atlas Trust

## 职责

Sentinel 是 Atlas 身份、访问令牌和签名密钥的唯一所有者。当前 access token 使用 Ed25519 密钥和
`alg=EdDSA`，有效期 15 分钟。Sentinel 发布只含公钥的 JWKS，不向 Gatehouse 或其他服务分发私钥。

当前策略来自 ADR-012，自 2026-05-31 生效，并取代 ADR-009 的 RS256 方案。RS256 文档仍保留用于
解释历史 token，但不能作为当前实现依据。

## 密钥轮换

- 正常轮换每 30 天执行。
- 新公钥必须在签发新 `kid` 前至少 10 分钟出现在 JWKS。
- 旧公钥在停止签发后保留 30 分钟，最低允许重叠窗口为 15 分钟。
- Gatehouse 的 JWKS 正常缓存 TTL 为 10 分钟，未知 `kid` 可触发一次提前刷新。
- 紧急吊销由 Incident Commander 和 Trust Foundations owner 双人批准。

## 消费者

Gatehouse 验证用户 access token；Relay 验证内部 delegated assertion；Foundry 的管理入口验证
短期 ingestion admin assertion。Prism、Polaris 和 Constellation 不直接解析用户 token，它们只接受
上游绑定后的 scope envelope。

## 数据与安全

密钥保存在 HSM-backed key store。日志可以记录 `kid`、issuer、audience 和失败原因，但不能记录完整
token、signature 或私钥材料。任何要求“导出生产签名密钥”的工单都必须拒绝。


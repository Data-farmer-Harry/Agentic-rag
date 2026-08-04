# ADR-009：使用 RS256 签署 Atlas Access Token

状态：Superseded by ADR-012  
生效：2025-11-15 至 2026-05-31  
Owner：Trust Foundations

## 历史决策

Sentinel 使用 2048-bit RSA key 和 `alg=RS256` 签署 30 分钟 access token。Gatehouse 每 15 分钟刷新
JWKS。轮换期间旧公钥保留 20 分钟。

## 被替代原因

RSA key 和签名体积较大，边缘验证成本高；30 分钟 token 生命周期也超过 2026 年 Trust review 的
目标。Architecture Council 在 ADR-012 中批准 Ed25519/EdDSA 和新的轮换协议。

## 使用限制

本文只用于解释 2026-05-31 之前的审计记录。任何询问“当前签名算法、token 生命周期或轮换窗口”
的问题都必须引用 ADR-012 和当前 Sentinel 服务说明，不能继续采用本文数字。


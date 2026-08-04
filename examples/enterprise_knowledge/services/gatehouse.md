# Gatehouse 服务说明

状态：Active  
版本：1.8  
Owner：Edge Platform  
值班：Atlas Edge

## 职责

Gatehouse 是 Atlas 唯一公网 API Gateway。它负责 TLS 终止、请求大小限制、租户级速率限制、request
ID、访问令牌验证和内部请求 envelope。Gatehouse 不保存会话正文、不执行知识检索，也不选择模型。

Gatehouse 从 Sentinel 的 `/jwks.json` 获取 Ed25519 公钥，并以 10 分钟 TTL 缓存在内存中。收到未知
`kid` 时允许立即刷新一次，但不得在每次请求中同步调用 Sentinel。验证成功后，Gatehouse 从 token
claims 绑定 `tenant_id/workspace_id/user_id`；客户端提交的同名 header 会被删除。

## 依赖

- Sentinel：JWKS 和 token contract。
- Relay：会话和任务的唯一上游。
- Redis-compatible limiter：短期租户限流计数；不保存认证状态。

## 公开路由

- `/v1/conversations/*` -> Relay
- `/v1/knowledge/uploads` -> Foundry
- `/health` 和 `/ready` -> Gatehouse 自身

Gatehouse 不把 Polaris、Constellation、Prism 或数据库地址暴露给客户端。

## 关键指标

- `gatehouse_request_duration_ms`
- `gatehouse_token_validation_failure_total{reason}`
- `gatehouse_jwks_refresh_total{result}`
- `gatehouse_rate_limit_rejection_total{workspace_id}`

## 常见误判

最终回答慢但 Gatehouse upstream connect 正常时，不应先扩容 Gatehouse。认证错误集中在未知 `kid` 时，
按 Sentinel JWKS rotation Runbook 处理；检索引用为空时由 Atlas Knowledge 排查。


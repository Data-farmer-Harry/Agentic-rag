# INC-2026-0527：JWKS 轮换导致认证失败

严重级别：SEV-1
状态：Closed
发生时间：2026-05-27 14:05-14:16 UTC
Incident Commander：Atlas Trust on-call

## 影响

EdDSA 上线前演练中，Gatehouse 对部分新签发 token 返回 401 `unknown_kid`。错误持续 11 分钟，影响
约 7% 的新会话请求。已有会话的 SSE 连接和 Polaris 检索未中断。Relay 的 delegated assertion 和
Foundry 管理入口也出现少量相同错误。

## 根因

轮换 job 在新 key 开始签发后立即从 JWKS 删除旧公钥，实际 overlap 为 0 分钟。Gatehouse 的 JWKS
缓存 TTL 是 10 分钟；缓存节点既没有新 key，又无法继续验证仍在有效期内的旧 token。演练脚本错误
地把“停止签发旧 key”和“删除旧公钥”合并成一步。

## 缓解

- Sentinel 暂停新 key 签发并重新发布旧公钥。
- Gatehouse 对 unknown `kid` 执行一次提前刷新。
- 14:16 所有验证错误恢复到基线。

## 长期修复

ADR-012 和 JWKS Rotation Runbook 2.1 固定以下门禁：新公钥预发布 10 分钟；停止旧 key 签发后继续
发布旧公钥 30 分钟，最低不得少于 15 分钟；删除旧 key 前检查 Gatehouse、Relay 和 Foundry 的
unknown-kid 指标。轮换 job 拆成 publish、activate、retire 三个带人工确认的阶段。

## 非根因

该事故不是 access token 过期时间过短，不是 Gatehouse rate limit，也不是 Prism 模型 provider 故障。


# Runbook：Sentinel JWKS 轮换

版本：2.1
状态：Active
Owner：Trust Foundations
最近演练：2026-07-22

## 正常轮换

1. 生成新 Ed25519 key，私钥只进入 HSM-backed store。
2. 将新公钥和新 `kid` 发布到 JWKS，保持 Sentinel 仍使用旧 key 签发。
3. 等待至少 10 分钟，确认 Gatehouse、Relay 和 Foundry 能看到新 `kid`。
4. 切换 Sentinel 签发 key，并记录 activate timestamp。
5. 旧公钥继续发布 30 分钟；最低 overlap 是 15 分钟，不能跳过。
6. 观察 `unknown_kid`、signature failure 和 JWKS refresh 指标。
7. overlap 满足且指标正常后 retire 旧公钥。

## 回滚

新 key 激活后 unknown-kid error 超过 0.5% 时，停止使用新 key 签发，重新确认旧公钥仍在 JWKS，并
通知 Atlas Edge、Atlas Agent 与 Knowledge Systems 的 Foundry owner。不得同时删除新旧公钥。

## 紧急吊销

只有确认 key compromise 时使用。需要 Trust Foundations owner 与 Incident Commander 双人批准。
紧急吊销允许不满足 overlap，但必须明确接受仍在有效期 token 失败的影响并发布事故通知。

## 验证对象

- Gatehouse：用户 access token。
- Relay：内部 delegated assertion。
- Foundry：ingestion admin assertion。

Polaris、Constellation 和 Prism 不解析 token，不需要刷新 JWKS。

## 历史教训

INC-2026-0527 中，轮换 job 将停止签发和删除公钥合并，造成 0 分钟 overlap 和 11 分钟 401。当前
publish、activate、retire 是三个独立阶段，retire 必须人工确认。


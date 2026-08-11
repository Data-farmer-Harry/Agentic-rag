# Runbook：模型 Provider 退化

状态：Active
版本：2.0
Owner：AI Runtime

## 触发条件

当 Prism 的 provider 429/5xx 超过 10%、首 token P95 超过 8 秒、online queue wait 超过 3 秒，或
`ProviderCircuitOpen` 告警触发时使用本 Runbook。先按 workload class 判断影响范围，不要直接重启 Relay。

## 前五步

1. 查看 `prism_request_total{provider,model,result,class}`，区分 online、ingestion 和 evaluation。
2. 检查 provider 状态页、配额、认证错误和网络；认证失败不可通过重试解决。
3. 暂停 evaluation，图谱抽取降到最多 12 并发，为 online 保留 70% 槽位。
4. 对 429/可恢复 5xx 打开 60 秒 circuit breaker，并允许少量探测；安全拒绝和 schema 错误不 fallback。
5. 若批准的 fallback 健康，按 ADR-007 切换 knowledge lane，并验证引用发布合同。

## 降级原则

conversation fast lane 与知识综合 lane 分开处理。知识模型不可用时，可以保留检索证据并明确告知暂时
无法综合；禁止用无引用模板冒充完整回答。Vision 请求不能自动降级到不支持图像的模型。restricted
workspace 不得切换到未批准的外部 provider。

## 恢复

Provider 连续 10 分钟错误率低于 2%、P95 恢复预算后，先将 5% 流量切回，观察 15 分钟再逐级恢复。
恢复批处理前确认 online queue wait 小于 500 ms。事件结束后保存时间线、模型 revision、失败分类和
用户影响，不保存完整 Prompt。

INC-2026-0712 的经验是：批量图谱回填必须与在线模型调用隔离，默认并发 12，最大 16。

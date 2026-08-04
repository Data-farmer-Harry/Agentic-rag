# Prism 模型路由服务说明

状态：Active  
版本：1.9  
Owner：AI Runtime  
值班：Atlas Runtime

## 职责

Prism 统一管理模型 lane、provider 预算、timeout、重试和允许的降级。业务服务不能直接携带 provider
secret，也不能通过请求参数指定任意模型。

## Lane

- `fast_conversation`：`orion-fast-v2`，用于问候、确认和轻量对话。
- `grounded_answer`：`orion-reasoner-v3`，用于有证据的研发问答。
- `structured_extraction`：`orion-structure-v2`，用于严格 schema 抽取。
- `vision_analysis`：`orion-vision-v2`，用于图片和扫描页。

## 降级规则

ADR-007 规定只有 timeout、429 和 provider 5xx 可以进入配置好的 fallback。安全拒答、内容策略拒绝、
schema 校验失败和证据不足不得通过更换模型绕过。重试总预算由 Relay run snapshot 冻结。

Prism 不决定是否检索。普通问候不检索是 Relay 的路由决定；Polaris 延迟也不应通过切换模型解决。

## 可观测性

记录 lane、model revision、provider category、duration、usage 和稳定错误码。不得记录 API key、完整
Prompt、私有文档正文或模型私有推理。


# Atlas 团队职责与值班归属

状态：Active  
周期：2026 Q3  
Owner：Engineering Operations

| 团队 | 拥有的服务 | 主要职责 | 值班队列 |
| --- | --- | --- | --- |
| Edge Platform | Gatehouse | 入口、限流、请求 envelope、token verification | Atlas Edge |
| Trust Foundations | Sentinel | 身份、签名密钥、JWKS、认证事件 | Atlas Trust |
| Agent Experience | Relay | 会话、路由、工具编排、答案发布 | Atlas Agent |
| Knowledge Systems | Polaris、Constellation、Foundry | 入库、检索、图谱和知识质量 | Atlas Knowledge |
| AI Runtime | Prism | 模型路由、provider 预算、重试和降级 | Atlas Runtime |

SRE Council 维护跨团队 SLO、严重级别和事故流程，但不拥有业务服务。Architecture Council 批准
跨团队 ADR，但不参与日常值班。

## 升级规则

- 单服务告警先进入该服务的值班队列。
- 同时影响 Gatehouse 和 Sentinel 的认证问题由 Atlas Trust 主导，Atlas Edge 协作。
- 检索或图谱问题由 Atlas Knowledge 主导；只有确认模型调用异常时才升级 Atlas Runtime。
- Relay 能返回但引用为空时，先检查 Polaris/Constellation，不先升级 Prism。
- 跨三个以上团队或持续超过 30 分钟的 SEV-1 由 SRE Council 指定 Incident Commander。

## 文档责任

每个团队必须每季度审阅服务说明和 Runbook。ADR 的 Owner 负责标记 superseded；新 ADR 生效后，
旧文档仍可保留用于历史解释，但不能继续作为当前操作依据。


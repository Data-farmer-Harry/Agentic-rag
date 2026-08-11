# Atlas 可观测性标准

状态：Active
版本：2026.08
Owner：SRE Council

## 关联标识

Gatehouse 创建 `request_id`，Relay 创建 `run_id`，Foundry 创建 `ingestion_job_id`。W3C `traceparent`
贯穿 HTTP 和队列事件；日志必须同时包含 scope、service、revision、trace ID 和阶段，但不得记录完整
用户输入、文档正文、token 或模型隐式推理。

## RED 与业务指标

所有在线服务提供 request rate、error rate、duration。Relay 额外记录 time-to-first-stage、工具次数、
停止原因和回答 confidence；Polaris 记录 dense/sparse/RRF 延迟、结果数和过滤命中；Foundry 记录各解析
阶段、队列年龄和 projection lag；Prism 记录 provider/model、首 token、token 数和归一化错误。

## Tracing

一个知识 Run 至少包含 route、retrieval、graph、model 和 publish span。工具输出只记录数量、字节、
evidence ID 哈希和状态。采样率正常为 5%，错误和超过 P95 的 trace 为 100%；restricted workspace 不得
导出 span attribute 到第三方 SaaS。

## 告警

告警基于用户症状和 SLO burn rate。快速窗口 5 分钟、慢速窗口 1 小时。单个实例 CPU 高只创建诊断，
不会自动 page；知识查询成功率、认证失败突增、outbox lag、active ingestion stuck 和 provider circuit
breaker 打开才触发值班。

## Dashboard

统一看板按 conversation、knowledge、graph、ingestion 四条用户旅程展示，不按微服务堆图。每个图表
必须能下钻到 revision 和 trace，并明确数据延迟。

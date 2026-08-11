# Prism 模型服务与路由工程

状态：Active
版本：2026.08
Owner：AI Runtime

## 路由目标

Prism 按任务能力、延迟预算、上下文长度、数据等级和成本选择模型。调用方提交的是 capability profile，
不能指定 provider secret 或任意模型名称。conversation fast lane 目标首 token 小于 800 ms；知识综合
lane 支持 Structured Outputs 和 Tool Calling；vision lane 只在输入包含需要解析的图像区域时启用。

## Provider 合同

所有 provider 适配器统一暴露 request ID、模型版本、input/output token、首 token、总耗时、finish reason
和归一化错误。429、可恢复 5xx 和网络超时可重试；认证失败、内容安全拒绝、schema 错误不自动换模型。
重试使用指数退避，单次 Run 最多跨 provider 尝试 3 次。

## 降级

知识回答降级时必须保留相同工具证据和 Answer schema。较弱模型若不能可靠发布 citations，则返回
partial 或安全失败，不能输出无引用长答案。安全策略拒绝不会 fallback；数据等级为 `restricted` 的请求
只允许内部批准 provider。

## 容量保护

Prism 按 workspace 使用 token bucket，并为交互流量保留 70% 并发。批量图谱抽取和离线评测使用独立
队列，不能耗尽在线槽位。Provider 429 比例连续 5 分钟超过 10% 时打开 circuit breaker 60 秒，期间只
探测少量请求。

## 版本记录

Run 必须记录逻辑路由 lane、实际 provider/model、prompt revision 和 schema revision。评测报告只能比较
这些版本完全声明的样本，禁止把模型别名当作固定二进制版本。

# Atlas Python 后端服务规范

状态：Active
版本：2026.08
Owner：Developer Productivity

## 分层

后端按 API、application service、domain、port 和 adapter 分层。Domain 不导入 FastAPI、数据库驱动或
provider SDK；API 只做身份绑定、输入校验和错误映射；application service 负责编排事务和能力；adapter
实现 PostgreSQL、Qdrant、Neo4j、Hermes 和模型接口。

## 类型与合同

公开边界使用严格 Pydantic model，禁止以无约束 `dict[str, Any]` 传播核心数据。所有时间使用 UTC aware
datetime，ID 使用 UUID，枚举值向后兼容。新增字段默认可选或有稳定默认；删除字段必须经过至少两个
发布周期。

## 异步规则

网络和数据库调用使用 async；CPU 密集 OCR、PDF 解析和 embedding batch 放到 worker。每个外部调用都
必须有 timeout、取消传播和归一化错误。禁止在 request coroutine 中使用无界 gather；并发由 semaphore
和总体 deadline 控制。

## 错误

领域错误映射为稳定 `code`，客户端只看到安全信息。401/403 不暴露策略细节，404 用于 scope 外资源，
409 表示版本或状态冲突，429 表示有界容量，503 表示关键依赖不可用。日志记录 exception class 和 trace
ID，不回显 provider body 或 secret。

## 质量门禁

Ruff、strict mypy、pytest 和 `pip check` 必须通过。共享行为需要合同测试；跨存储逻辑至少覆盖部分失败、
幂等重试和归档。代码注释只解释不明显的不变量。

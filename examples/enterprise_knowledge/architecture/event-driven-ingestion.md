# Atlas 事件驱动摄取架构

状态：Active
版本：2026.08
Owner：Knowledge Systems

## 目标

Atlas 的文档摄取采用事务性 outbox 驱动的异步流水线。目标是让 PostgreSQL 中的 Document/Chunk 真相、
Qdrant 索引和 Neo4j 投影最终收敛，同时避免把跨存储写入伪装成单个原子事务。

## 处理阶段

1. Gatehouse 将上传流交给 Foundry，Foundry 计算 SHA-256 并写入对象存储。
2. Parser 生成 Document IR；Chunker 按标题层级和 700 token 目标预算切分。
3. 一个 PostgreSQL 事务写入 Document、Chunk、IngestionJob 和 `knowledge.index.requested` outbox 事件。
4. Dispatcher 使用 `event_id` 作为幂等键，分别更新 Qdrant point 和 Neo4j 结构投影。
5. 两个投影都确认后，Job 才从 `indexing` 进入 `ready`。

事件至少包含 `event_id`、`tenant_id`、`workspace_id`、`document_id`、`revision` 和 `content_hash`。消费者
不得依赖消息到达顺序；同一文档只接受 revision 单调递增的事件。旧 revision 的迟到事件记录为
`stale_event`，不能重新激活归档 Chunk。

## 重试与死信

Dispatcher 使用指数退避加抖动，重试间隔上限 15 分钟。连续 12 次失败后进入 dead-letter 状态并触发
`FoundryProjectionStalled` 告警。重放必须沿用原 `event_id`，禁止生成新事件绕过幂等约束。

## 一致性边界

PostgreSQL 是文档状态真相源；Qdrant 和 Neo4j 是可重建投影。在线检索只读取 `ready` revision。若
Qdrant 已成功而 Neo4j 尚未完成，直接事实检索可以继续，但图谱关系回答必须标记 partial。Reconciler
每 30 分钟比较 active Chunk 数、Qdrant point 数和 Neo4j `Chunk` 节点数，并生成差异报告。

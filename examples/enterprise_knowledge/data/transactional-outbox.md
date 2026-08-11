# Atlas Transactional Outbox 合同

状态：Active
版本：2026.08
Owner：Data Foundations

## 事件格式

所有跨存储副作用先写 `outbox_events`。事件字段包括 UUID `event_id`、聚合类型、聚合 ID、scope、
revision、事件类型、JSON payload、创建时间、发布时间和尝试次数。payload 使用版本化 schema，例如
`knowledge.chunk.index.requested.v2`，消费者必须拒绝未知 major version。

## 发布语义

Dispatcher 使用 `FOR UPDATE SKIP LOCKED` 批量领取未发布事件，每批最多 100 条。成功后写
`published_at`；失败只更新尝试次数和安全错误码。该机制提供 at-least-once，而不是 exactly-once，
因此 Qdrant、Neo4j 和学习 worker 都必须用 `event_id` 或目标 revision 幂等。

同一聚合的事件可能因 worker 并发乱序到达。消费者保存最后 applied revision：较新的 revision 先到时，
后到的旧事件标记 stale；相同 revision 与 content hash 视为重复成功；相同 revision 不同 hash 进入
quarantine 并报警。

## 补偿与对账

跨存储部分失败不回滚已经提交的 PostgreSQL 事务。Reconciler 根据 active Document/Chunk 真相重新发出
缺失投影请求；归档事件优先级高于普通索引事件。未发布事件超过 5 分钟触发 warning，超过 30 分钟触发
critical。运维重放必须保留原 event ID 和审计人。

## 保留策略

已发布事件在线保留 14 天后压缩到审计对象存储；失败和 quarantine 事件保留 90 天。事件 payload 不得
包含完整文档、access token、provider key 或未脱敏用户输入。

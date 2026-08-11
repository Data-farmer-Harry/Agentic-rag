# Atlas 数据存储边界与所有权

状态：Active
版本：2026.08
Owner：Architecture Council

## 存储分工

- PostgreSQL：Document、Chunk 元数据、会话、Run、Memory、Skill、Job、outbox 和审核状态的真相源。
- Qdrant：Chunk 的 dense/sparse 检索投影，不保存最终业务状态。
- Neo4j：结构节点和已批准语义关系的遍历投影，不承担文档版本真相。
- 对象存储：上传原件、Document IR 和可重建中间产物。
- Redis-compatible limiter：Gatehouse 短期限流计数，不得存储认证会话或长期记忆。

## 写入所有权

Foundry 是 Document/Chunk、Qdrant point 和结构图投影的唯一生产者。Relay 是 conversation/run/event 的
唯一写入方。Learning Worker 只能通过受控 repository 写 Memory、Skill 和 ChangeSet。任何服务都不得
直接修改另一个服务拥有的表，也不得让模型生成 SQL、Cypher 或 Qdrant filter 后直接执行。

## 标识与隔离

所有持久对象必须携带 `tenant_id` 和 `workspace_id`。个人层对象还必须携带 `user_id`。Document 使用
稳定 `source_id` 标识逻辑来源，用 `document_id` 标识具体 revision；Chunk ID 由 document revision 与
chunk ordinal 派生。跨租户 join 和没有 scope filter 的向量查询在 repository 层失败关闭。

## 删除和归档

用户删除默认执行逻辑归档：PostgreSQL 状态先变为 archived，再由 outbox 清理 Qdrant 与 Neo4j 投影。
法律删除任务会在 30 天恢复窗口后清除对象原件和内容字段，但保留不含正文的审计摘要。删除尚未收敛
时，在线检索以 PostgreSQL active 状态二次过滤，避免迟延向量继续进入回答。

## 禁止模式

禁止以 Qdrant point 数量推断文档是否 active，禁止把 Neo4j 节点当成访问控制主体，禁止在对象存储
路径中放 token 或用户邮箱，也禁止通过共享数据库表绕过服务合同。

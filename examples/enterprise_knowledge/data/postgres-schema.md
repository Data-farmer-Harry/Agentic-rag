# Atlas PostgreSQL 核心数据模型

状态：Active
版本：2026.08
Owner：Data Foundations

## 核心表

`knowledge_documents` 保存逻辑来源、revision、状态和内容哈希；`knowledge_chunks` 保存标题路径、token
区间、正文和 document 外键；`runs` 与 `run_events` 保存 Agent 终态和可重放 SSE 游标；`memories`、
`skills`、`learning_changesets` 保存自学习资产；`ingestion_jobs` 与 `outbox_events` 驱动异步投影。

所有租户表以 `(tenant_id, project_id, id)` 为主访问前缀。个人数据额外建立
`(tenant_id, project_id, user_id, status)` 索引。`source_id + source_revision` 在同一作用域内唯一，
active revision 使用部分唯一索引保证同一逻辑来源最多一个当前版本。

## 事务边界

文档元数据、Chunk 和 outbox 事件在同一事务提交。Qdrant/Neo4j 不参与 PostgreSQL 事务。Run 发布答案
时，终态、引用 ID、memory ID 和最后公开事件一起提交；Learning Job 在独立事务创建，避免反思延迟
阻塞用户响应。

## 并发控制

可变聚合使用整数 `version` 执行 compare-and-swap。Job worker 通过 `FOR UPDATE SKIP LOCKED` 领取任务，
租约 60 秒，每 20 秒续约；过期租约可被其他 worker 回收。fixture generation 和 source replacement
必须在提交前重新检查版本，防止 reset 与迟到 finalizer 互相覆盖。

## 运维要求

连接池每个应用实例上限 20，事务空闲超时 30 秒，慢查询阈值 500 ms。迁移必须先执行 expand，再发布
兼容代码，最后 contract；禁止在高流量时对大表执行无界默认值重写。每日校验外键、active 唯一性和
未发布 outbox 数量。

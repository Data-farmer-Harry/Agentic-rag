# Atlas 数据保留与备份策略

状态：Active
版本：2026.08
Owner：SRE Council

## 保留周期

会话正文默认保留 180 天，Run 轨迹保留 90 天，脱敏审计事件保留 365 天。active 团队文档按业务生命周期
保留；superseded revision 在线保留 180 天后转冷存储。撤回 Memory 立即停止注入 Prompt，30 天后清除
正文，仅保留撤回时间和哈希审计。

上传原件、Document IR 和 Chunk 必须共享同一 retention class。归档文档在 30 天恢复窗口后从 Qdrant
与 Neo4j 清除，随后清除对象正文；禁止只删 PostgreSQL 标记而长期遗留可检索向量。

## 备份

PostgreSQL 每日全量备份并持续归档 WAL，保留 35 天；Qdrant 每 6 小时 snapshot，保留 14 天；Neo4j
每日全量、每小时增量，保留 14 天；对象存储启用版本控制和 30 天删除保护。备份使用独立 KMS key，
恢复角色与生产写角色分离。

## 恢复验证

备份成功不等于可恢复。每周在隔离 namespace 恢复 PostgreSQL，每月联合恢复 Qdrant 和 Neo4j。验证项
包括 schema migration 版本、Document/Chunk 外键、随机 content hash、Qdrant payload index、图关系
证据引用，以及 Atlas 企业 10-case 回归集。

## 删除请求

法律删除通过可审计 job 执行，按 source/document/user scope 传播到所有存储。任务完成前 API 返回
`deletion_pending`，不得宣称已彻底删除。备份中的数据通过过期淘汰，不进行不可验证的就地修改。

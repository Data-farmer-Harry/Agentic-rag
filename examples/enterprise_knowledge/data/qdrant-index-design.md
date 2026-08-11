# Atlas Qdrant 混合索引设计

状态：Active
版本：2026.08
Owner：Knowledge Systems

## Collection

当前别名 `atlas_chunks_active` 指向 `atlas_chunks_v3_idf`。每个 point 同时包含 1536 维 dense vector 和
IDF-aware sparse vector。payload 至少包含 `tenant_id`、`workspace_id`、`user_id`、`knowledge_layer`、
`document_id`、`source_id`、`status`、`source_revision`、`trust` 和 `content_hash`。

`tenant_id`、`workspace_id`、`knowledge_layer`、`status`、`document_id` 必须建立 payload index。发布前
preflight 若发现任何必需索引缺失则失败，不允许退化成全量 payload 扫描。INC-2026-0218 就是因为
`workspace_id` index 在 v3 迁移中遗漏，P95 上升到 9.4 秒。

## 查询

Polaris 分别提交 dense 与 sparse prefetch，由 Qdrant server-side RRF 合并。默认每路 prefetch 40，
融合后取 12，再由作用域、版本、trust 和多样性规则筛选为最多 8 条证据。同一文档默认最多保留 3 个
Chunk，避免长文档垄断结果。

## 版本升级

embedding 或 sparse 算法变更必须创建新 collection，执行双写、离线评测、影子查询和别名原子切换。
禁止原地覆盖不同向量空间。切换后旧 collection 保留 7 天；回滚只切换 alias，不重新计算 point。

## 容量与恢复

目标 shard 大小不超过 25 GB，replication factor 为 2，write consistency factor 为 2。每 6 小时生成
snapshot 并复制到对象存储。恢复验收必须检查 point 数、payload index、随机 100 个 content hash 和
10-case 企业检索门禁。

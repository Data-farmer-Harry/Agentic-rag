# Polaris 检索服务说明

状态：Active  
版本：3.3  
Owner：Knowledge Systems  
值班：Atlas Knowledge

## 职责

Polaris 为 Atlas 提供团队、个人和公共参考知识的检索。当前 active collection 是
`atlas_chunks_v3`。每个 point 必须包含 `tenant_id`、`workspace_id`、`document_id`、`source_layer`、
`status` 和 revision metadata。

## 检索策略

Polaris 按 ADR-003 执行 dense 与 sparse 两路召回，再使用 Qdrant server-side Reciprocal Rank Fusion。
原始用户查询必须至少保留一个锚定检索；比较任务可以拆分对象查询。Polaris 不使用固定单向量 top-k
作为唯一结果，也不让模型生成任意数据库过滤表达式。

默认 source 优先级是 active 团队资料、当前用户资料、公共参考资料。`superseded` 文档可在历史查询
中返回，但普通“当前是什么”问题必须优先 active revision并标明替代关系。

## 存储依赖

- Qdrant：dense/sparse point 和 workspace payload filter。
- PostgreSQL：Document/Chunk 元数据、revision、来源和 active 状态。
- Foundry：唯一索引生产者。

Polaris 不直接依赖 Neo4j。需要关系路径时由 Relay 调用 Constellation，并把 Polaris 证据作为图查询
锚点。

## 运行门槛

- direct lookup P95 小于 800 ms。
- 每次最多四个子查询和两轮检索。
- workspace filter 必须命中 payload index；无 index 时迁移 preflight 必须失败。
- 归档文档的 point 不能进入 active 查询。

## 事故关联

INC-2026-0218 的根因是 `atlas_chunks_v3` 迁移时遗漏 `workspace_id` payload index，导致过滤扫描和
P95 9.4 秒。该事故与 embedding model、Prism provider 或 Neo4j 无关。


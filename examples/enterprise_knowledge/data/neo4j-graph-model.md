# Atlas Neo4j 知识图谱模型

状态：Active
版本：2026.08
Owner：Knowledge Systems

## 节点层次

图中包含 `Document`、`Chunk`、`Entity` 和受控业务标签 `Service`、`Team`、`Database`、`Decision`、
`Incident`、`Runbook`。Document/Chunk 属于结构层，可由 Foundry 幂等重建；业务实体和语义关系来自
审核通过的候选或人工策展 seed。

每个节点携带 `tenant_id`、`project_id`、`knowledge_layer` 和稳定业务键。实体名称不是全局唯一，唯一
约束使用 `(tenant_id, project_id, entity_key)`。个人层节点额外携带 `user_id`，查询缺少 user context
时失败关闭。

## 关系

结构关系包括 `HAS_CHUNK`、`MENTIONS` 和 `EVIDENCED_BY`；语义关系统一标记为
`SEMANTIC_RELATION`，并用 `relation_type` 表示 `depends_on`、`owned_by`、`calls`、`supersedes`、
`affected`、`mitigated_by` 等。每条语义关系必须关联至少一个 active Chunk 证据和审核 revision。

## 查询约束

Constellation 只执行预定义 traversal，不接受任意 Cypher。默认最大深度 4、最多 50 条路径、每条关系
最多投影 5 个证据 Chunk。路径先按 scope、状态和 layer 限制，再 join 证据，避免先遍历全图后过滤。

## 候选治理

LLM 或规则抽取只产生 pending candidate。批准时解析实体、检查重复关系和证据状态，再写语义图；拒绝
候选保留审计记录但不进入普通查询。文档归档后，失去全部 active 证据的关系必须从 active traversal
中隐藏，Reconciler 每小时检查孤儿关系。

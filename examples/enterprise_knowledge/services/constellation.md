# Constellation 图谱服务说明

状态：Active  
版本：2.0  
Owner：Knowledge Systems  
值班：Atlas Knowledge

## 职责

Constellation 提供研发知识图谱的实体解析、邻居、最短证据路径、影响分析和实体比较。主要实体包括
Service、API、Team、Decision、Incident、Runbook 和 Document。

## 查询边界

客户端和 Relay 只能选择固定模板：

- `neighbors`
- `paths`
- `impact`
- `ownership`
- `incident_context`
- `compare`

Constellation 不接受任意 Cypher。所有 Neo4j 查询由服务端参数化并强制 tenant/workspace filter，最大
三跳。普通查询只遍历 active、approved 关系；pending、rejected、archived 和 superseded 当前事实不
进入结果。

## 证据合同

每条可返回关系必须携带一个或多个 source Chunk ID。关系路径本身不是答案；Relay 只能在关系两端和
边都有可读取证据时发布关系结论。若图可用但支持 Chunk 已归档，路径应视为不可发布。

## 典型路径

- `Polaris -> OWNED_BY -> Knowledge Systems`
- `Relay -> CALLS -> Polaris`
- `INC-2026-0218 -> AFFECTED -> Polaris`
- `INC-2026-0218 -> MITIGATED_BY -> Retrieval Degradation Runbook`
- `ADR-012 -> SUPERSEDES -> ADR-009`

## 依赖

Constellation 使用 Neo4j 作为可重建查询投影，候选审核真相源不放在 Neo4j。Foundry 生产结构关系和
pending candidate；审核控制面批准语义关系后才投影为 active。


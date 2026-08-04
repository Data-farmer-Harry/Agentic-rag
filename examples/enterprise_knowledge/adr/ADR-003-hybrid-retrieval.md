# ADR-003：采用 Dense + Sparse + RRF 混合检索

状态：Accepted  
日期：2026-01-12  
Owner：Architecture Council

## 背景

Atlas 团队知识同时包含自然语言说明、稳定服务名、事故编号、API path 和内部缩写。仅 dense 检索对
语义改写有效，但会漏掉精确编号；仅关键词检索能找到编号，却难以处理“鉴权服务”等同义表达。

## 决策

Polaris 使用 dense embedding 与 sparse term 两路召回，并在 Qdrant 服务端使用 Reciprocal Rank
Fusion。所有候选必须先执行 tenant/workspace/status filter。原始查询至少执行一次，模型改写不能完全
替代用户输入。

关系、负责人、影响和事故链问题由 Relay 在文本证据基础上调用 Constellation。Neo4j 不替代文本
检索，图路径也必须回连 source Chunk。

## 预算

- 首轮最多四个子查询。
- 最多两轮检索。
- 直接 lookup 默认 top 10。
- 没有新增证据、证据充分、达到预算或用户取消时停止。

## 结果

正面：兼顾内部标识符和语义改写，支持有界补检。代价：需要维护两种索引、融合评测和 payload
index。该决策不包含学习型 reranker；引入 reranker 必须单独通过离线和在线门禁。


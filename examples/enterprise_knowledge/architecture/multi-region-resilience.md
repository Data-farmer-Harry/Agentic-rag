# Atlas 多可用区与区域恢复设计

状态：Active
版本：2026.08
Owner：SRE Council

## 部署范围

Atlas 当前生产环境在 `cn-east-1` 单区域、三个可用区运行。Gatehouse、Relay、Polaris、Constellation、
Foundry 和 Prism 都是无状态 Kubernetes Deployment，跨可用区分散；PostgreSQL 使用一主两同步副本，
对象存储和 Qdrant 使用跨可用区复制。Neo4j 使用三成员集群。

## 可用区故障

单个可用区丢失时，PodDisruptionBudget 保证 Gatehouse、Relay 和 Polaris 至少各保留两个实例。
PostgreSQL 自动提升同区域同步副本，目标 RTO 为 5 分钟，RPO 为 0。Qdrant shard replica factor 为 2，
写入 consistency factor 为 2；若只剩一个副本，知识摄取暂停但已有索引仍可只读查询。

## 区域级灾难

`cn-north-1` 是 warm standby。PostgreSQL 每 5 分钟传输 WAL，目标 RPO 小于 5 分钟；对象存储启用异步
跨区域复制；Qdrant 每 6 小时生成 collection snapshot；Neo4j 每日全量、每小时增量备份。区域切换由
Incident Commander 与 SRE owner 双人批准，DNS TTL 为 60 秒，目标 RTO 为 60 分钟。

## 降级顺序

区域恢复期间优先恢复 Sentinel、Gatehouse、Relay 和 PostgreSQL，随后恢复 Polaris/Qdrant，再恢复
Constellation/Neo4j，最后恢复 Foundry 写入和离线评测。图谱不可用时允许证据文本问答；Qdrant 不可用
时只允许会话和运维入口，不能把 PostgreSQL 模糊查询包装成完整 RAG。

每季度执行一次只读恢复演练，每半年执行一次受控 DNS 切换。演练必须记录实际 RTO/RPO、校验 20 个
知识查询和 5 条图谱路径，并验证旧区域恢复后不会双写。

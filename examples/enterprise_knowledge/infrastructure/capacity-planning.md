# Atlas 容量规划与性能预算

状态：Active
版本：2026.08
Owner：Performance Engineering

## 基准负载

容量模型以工作日峰值 120 RPS、并发 600 个会话 Run、每小时 2,000 份文档摄取为基线，并预留 2 倍
突发。conversation、knowledge、graph 和 ingestion 分开建模，不能用平均 RPS 掩盖模型长尾和批处理。

## 在线预算

Gatehouse 自身 P95 预算 80 ms，Relay 路由 150 ms，Polaris direct lookup 800 ms，Constellation 两跳
查询 500 ms。conversation 端到端 P95 目标 2 秒，直接知识问答 15 秒，多跳综合 30 秒。Provider 时间
单独展示，不计入内部服务 SLO 但计入用户端到端 SLI。

## 存储容量

Qdrant 单 shard 目标不超过 25 GB，超过 70% 容量开始扩容；PostgreSQL 连接使用率持续超过 70% 时先
排查长事务和 N+1，再调整 pool；Neo4j page cache 目标覆盖 active graph store 的 80%。对象存储按原件、
IR、备份分别计费与保留。

## 压测方法

使用脱敏或合成文档，保持真实 Chunk 长度、语言和过滤基数。压测必须包含 20% 多轮对话、50% 直接
知识、20% 多跳图谱、10% 上传，并注入 provider 429、Qdrant 慢 shard 和 PostgreSQL failover。

## 扩缩限制

HPA 扩容不能突破 PostgreSQL、provider 和 Qdrant 的总并发预算。批量 embedding 在在线队列等待超过
1 秒时自动降速。容量结论每季度复审，任何模型或 embedding 版本切换后重新标定。

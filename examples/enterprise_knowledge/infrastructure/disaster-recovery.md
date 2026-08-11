# Atlas 灾难恢复 Runbook 设计

状态：Active
版本：2026.08
Owner：SRE Council

## 恢复等级

SEV-1 包括主区域不可达、PostgreSQL 不可恢复损坏、跨租户数据暴露或认证系统全面不可用。Incident
Commander 宣布灾难模式后冻结发布和非必要摄取，所有操作记录到独立事件日志。

## 恢复顺序

1. 恢复 Sentinel 和 Gatehouse，验证 EdDSA/JWKS 与 owner 运维访问。
2. 恢复 PostgreSQL 到最后可验证 WAL 点，检查 migration version 和 scope 约束。
3. 启动 Relay 与 Prism，先开放 conversation lane。
4. 恢复 Qdrant snapshot，校验 payload index 后开放 Polaris。
5. 恢复 Neo4j 并执行证据引用检查，再开放 Constellation。
6. 最后恢复 Foundry、outbox dispatcher 和 Learning Worker。

## 防止脑裂

区域切换前必须 fencing 旧 PostgreSQL writer 和旧 outbox dispatcher。DNS 切换不是写权限切换；只有
新的数据库 lease holder 可以接受写入。旧区域恢复后以只读方式加入，完成差异检查前禁止双向复制。

## 验收

执行 5 个普通会话、10 个企业 RAG 问题、5 条图路径、一次上传和一次 Memory 撤回。核对 Document、
Chunk、Qdrant point、图证据、未发布 outbox 和 active Job。任何 scope 查询异常都视为恢复失败。

## 目标

单可用区 RTO 5 分钟/RPO 0；区域灾难 RTO 60 分钟、PostgreSQL RPO 小于 5 分钟，Qdrant 最坏 RPO 6
小时。演练实测超过目标必须创建容量或架构改进项，而不是修改报告目标。

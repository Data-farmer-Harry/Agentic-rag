# INC-2026-0712：图谱回填挤占在线模型配额

状态：Resolved
级别：SEV-2
Owner：AI Runtime / Knowledge Systems

## 影响

2026-07-12 09:18 至 09:46，Atlas 多跳知识问答 P95 从 18 秒升至 71 秒，约 7.6% 的 Run 因 provider
timeout 失败。普通 conversation fast lane 基本正常，文档检索结果和数据库没有丢失。

## 根因

Knowledge Systems 启动 48 并发的图谱实体关系回填。该批任务与 Prism 在线知识综合共享同一 provider
project 和并发池，瞬时占用全部 64 个槽位。Prism 虽有 workspace 限流，但没有按 workload class 预留
在线容量，导致 Relay 请求排队并超时。

问题与 Neo4j 写入性能无关：事发期间 Neo4j commit P95 为 42 ms，Qdrant lookup P95 为 310 ms，
PostgreSQL 连接使用率为 48%。直接原因是模型并发隔离缺失。

## 缓解

09:27 将回填并发从 48 降到 8；09:31 暂停新的 extraction job；09:38 Prism 为 online lane 临时预留
40 个槽位。09:46 延迟恢复。

## 永久修复

- Prism 按 online、ingestion、evaluation 三个 workload class 建独立 semaphore。
- online 保留至少 70% provider 并发；批处理根据在线队列延迟自动降速。
- 图谱回填默认并发 12，未经容量评审不得超过 16。
- 新增 `prism_queue_wait_ms{class}` 和 provider saturation 告警。
- 发布流水线加入“批任务不能影响 conversation P95”的故障注入测试。

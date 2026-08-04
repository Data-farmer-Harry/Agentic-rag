# INC-2026-0218：Polaris workspace 检索延迟

严重级别：SEV-1  
状态：Closed  
发生时间：2026-02-18 09:42-10:31 UTC  
Incident Commander：Atlas Knowledge on-call

## 影响

Atlas 团队知识问题的检索 P95 从 720 ms 上升到 9.4 秒，约 31% 的 knowledge run 超过 Relay 的第一轮
检索预算。普通 conversation lane、Sentinel 认证和 Foundry 上传未受影响。受影响范围是使用新 Qdrant
collection `atlas_chunks_v3` 的 18 个 workspace。

## 时间线

- 09:30：完成 `atlas_chunks_v2` 到 `atlas_chunks_v3` 的流量切换。
- 09:42：`polaris_retrieval_duration_ms` P95 告警。
- 09:48：确认 dense 和 sparse embedding 延迟正常，provider 无异常。
- 09:56：发现带 `workspace_id` filter 的查询慢，无 filter 的只读诊断查询正常。
- 10:04：检查 collection schema，确认 `workspace_id` payload index 缺失。
- 10:17：在线创建 keyword payload index，并验证过滤查询恢复。
- 10:31：P95 降到 760 ms，事件关闭。

## 根因

collection migration 创建了 `tenant_id`、`status` 和 `source_layer` index，但迁移模板遗漏
`workspace_id` payload index。Polaris 的每个请求仍正确携带 workspace filter，Qdrant 因缺少 index
执行高成本过滤扫描。根因不是 embedding model、Prism provider、Neo4j 或请求量增长。

## 缓解

当班工程师按 Retrieval Degradation Runbook 确认 filter-specific latency，创建缺失 index，并暂时把
每轮 prefetch 从 80 降到 40。在 index ready 后恢复 prefetch。

## 长期修复

1. collection migration preflight 必须验证全部 required payload index。
2. shadow collection 只有通过 scope filter latency probe 后才能切 active alias。
3. 增加 `qdrant_filter_index_missing` 启动门禁。
4. Runbook 把 workspace index 检查放到模型/provider 排查之前。

## 关联

- 服务：Polaris。
- 存储：Qdrant `atlas_chunks_v3`。
- Runbook：Polaris 检索退化。
- ADR：ADR-003 混合检索。


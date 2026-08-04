# Runbook：Polaris 检索退化

版本：3.2  
状态：Active  
Owner：Knowledge Systems  
最近演练：2026-07-25

## 触发条件

- `polaris_retrieval_duration_ms` P95 连续 5 分钟超过 1.5 秒。
- knowledge run timeout 超过 3%。
- 引用数量突然下降且入库状态正常。

## 前三项检查

1. **按 query type 和 filter 拆分延迟。** 比较带 `workspace_id` filter 与受控无 filter 诊断查询，确认
   问题是否特定于 scope filter。
2. **检查 Qdrant collection 与 payload index。** active alias 应指向 `atlas_chunks_v3`；
   `tenant_id`、`workspace_id`、`status`、`source_layer` 必须存在 index。
3. **检查 Foundry/outbox 新鲜度。** 确认没有大量 pending/dead-letter，active Document 数与 Qdrant
   active point 数没有异常偏差。

前三项正常后，再检查 dense/sparse encoder latency、Qdrant CPU/IO、Polaris prefetch 和 Relay 子查询
数量。只有明确看到 Prism lane 或 provider 错误时才升级 Atlas Runtime。

## 安全降级

- Neo4j 不可用时，直接事实问答可以继续文本检索；关系结论必须标记不可用。
- 单一路召回失败时可以使用另一可用路，并在 run 中标记 partial。
- 可以临时降低 prefetch，但不能移除 tenant/workspace filter。
- 禁止用全局无 filter 查询替代生产请求。

## INC-2026-0218 特征

如果无 filter 查询正常、workspace filter 查询极慢，首先检查 `workspace_id` payload index。创建 index
前确认 collection 和字段类型；index ready 后运行 scope isolation probe，再恢复正常 prefetch。

## 结束条件

P95 连续 15 分钟低于 800 ms、timeout 低于 1%、scope probe 全部通过，并记录根因或进入问题单。


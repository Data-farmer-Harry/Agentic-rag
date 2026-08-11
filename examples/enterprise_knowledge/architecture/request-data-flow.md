# Atlas 请求与知识数据流

状态：Active
版本：2026.06

## 在线请求

1. Gatehouse 接收 `POST /v1/conversations/{conversation_id}/messages`。
2. Gatehouse 校验 body 大小、request ID、租户限流和 EdDSA access token。
3. Gatehouse 将服务端绑定的 `tenant_id/workspace_id/user_id` 放入内部 envelope。
4. Relay 读取最近 8 轮有界会话历史并执行路由。
5. 普通寒暄直接走 Prism 的 `orion-fast-v2` lane，不调用 Polaris。
6. 团队知识问题由 Polaris 查询 `atlas_chunks_v3`，同时使用 dense、sparse 与 server-side RRF。
7. 依赖、负责人、事故关系或影响分析由 Constellation 执行 allowlisted traversal。
8. Relay 只把通过证据白名单的引用交给模型，并发布最终回答。

Relay 最多执行两轮知识检索。第二轮只在缺少必需术语、来源不足或比较对象不完整时发生。达到预算、
证据充分、用户取消或剩余查询不能增加证据时停止。

## 文档摄取

1. Gatehouse 把上传流转发给 Foundry ingestion endpoint。
2. Foundry 保存原始对象并计算 SHA-256。
3. Parser 生成标题、章节、段落、表格和图片区域组成的 Document IR。
4. Chunker 按标题层级和 token 预算切分，相邻 Chunk 只保留有界 overlap。
5. PostgreSQL transaction 写入 active Document/Chunk 和 outbox event。
6. Dispatcher 幂等更新 Qdrant 与 Neo4j 结构层。
7. 实体关系抽取结果进入 pending candidate；只有审核通过后进入普通图谱查询。

## 失败语义

- Qdrant 暂时失败：在线回答可降级到可用检索器并标记 partial，不伪装成全量结果。
- Neo4j 暂时失败：直接事实问答可以继续；关系结论必须省略并说明图谱不可用。
- Prism provider 429/5xx/timeout：按 ADR-007 选择允许的 fallback；安全拒答不能 fallback。
- Foundry 入库失败：文档保持 failed，不显示为可检索。
- 用户取消：Relay 停止后续工具，保存 cancelled 终态，不删除既有会话。

## 可观测关联

`request_id` 由 Gatehouse 生成，`run_id` 由 Relay 生成，`ingestion_job_id` 由 Foundry 生成。三者不能
互相替代。所有日志只记录 scope ID、阶段、状态、耗时和脱敏摘要，禁止记录 access token 或完整文档。


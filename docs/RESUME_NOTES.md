# HermesGraph 简历与面试表述

最后更新：2026-07-28

## 简历项目名称

**HermesGraph：基于 Hermes、OpenAI SDK、LangChain、Neo4j 与 Qdrant 的自进化多模态个人 Agent**

## 可直接使用的项目描述

- 设计并实现 evidence-first GraphRAG Tool Suite，将实体别名消歧、多跳证据子图检索和实体关系对比
  封装为 Hermes 可调用工具；支持 1-3 hop 连接路径、共享/独有邻居分析及文本-图谱证据融合。
- 基于 Neo4j 构建 tenant/project 隔离的知识图谱读取层，采用固定 Cypher 模板、参数化查询、active
  候选门禁与 Chunk evidence join；应用层二次拒绝跨 scope、缺证据和端点不完整的路径，禁止 Agent
  自由生成 Cypher。
- 以 LangChain Integration Runtime 统一 Qdrant Hybrid Retrieval 与 Neo4j Graph Retrieval，通过
  versioned Capability Registry 实施 Pydantic/JSON Schema、`graph:read + knowledge:read` 权限、timeout、
  输出字节和 provenance 合同。
- 实现 run-scoped Agent 工具治理：全局/图谱调用预算、重复输入 fingerprint、HMAC bridge、工具事件
  审计和 evidence allowlist；模型只能引用本轮工具真实返回的证据 ID，由服务端补全最终 citation。
- 构建知识入库与图谱控制面：结构化实体/关系抽取、稳定候选 ID、人工审核、跨文档 `same_as` 归并、
  Qdrant/Neo4j 补偿归档；以 checkpointed Postgres-to-Neo4j 重投影和双存储 evidence reconciliation
  治理 chunk revision，已管理 528 篇、43,872 chunks 和 11,023 页完整文本/Vision OCR 语料。
- 实现安全只读 Computer Workspace Toolset：显式 root/scope、路径与 symlink 逃逸阻断、凭据文件
  过滤、PDF/DOCX/XLSX 本地解析和分层预算；读取结果转换为 run-scoped evidence 并进入同一引用门禁。
- 打通受治理 Skill 的在线生效路径：run start 冻结相关 Skill index，Hermes 通过
  `activate_governed_skill` 获取同一 snapshot 的精确 Canary/Active 声明式步骤；激活事件进入
  health gate，不执行脚本或授予新权限。

## 30 秒架构说明

Hermes 负责唯一的 Agent Loop，OpenAI Python SDK 负责 Responses、Structured Outputs、Vision 和
Embeddings，LangChain 是有界的数据流与能力衔接层。Agent 调用语义化 GraphRAG 工具后，
Integration Runtime 从 Qdrant 召回文本证据、从 Neo4j 解析实体并扩展固定深度路径，再按 provenance
identity 去重。所有 scope 都来自服务端 RunContext，每条图关系必须回连来源 Chunk，最终回答只能
引用本轮 evidence allowlist，因此检索路径和答案都可审计。

## 高频追问

**为什么不用 LLM 直接生成 Cypher？**

Cypher 会同时控制深度、label、关系、scope、limit 和 evidence join，提示词无法承担数据库授权边界。
系统让 Agent 选择“解析、子图、对比、冲突”等检索意图，但查询结构由预编译模板控制，在保留
agentic 决策能力的同时缩小注入、越权、笛卡尔积和不可回放风险。

**为什么同时需要向量库和知识图谱？**

向量检索适合语义相似的非结构化证据，知识图谱适合关系约束、多跳路径、共同邻居和冲突结构。
联合工具先并行做文本召回与实体解析，再从解析实体扩展证据路径；回答拿到的是两类证据的去重并集，
而不是用图谱替代全文语义检索。

**实体消歧怎么做？**

当前在线 resolver 对 canonical name 与 aliases 做确定性双向匹配和固定评分，并支持 entity type 与
阈值过滤；Neo4j 结果必须 join 到 active Chunk evidence。这样可回放、低成本，适合在线 seed linking。
embedding entity linker 或 learned reranker 是可替换的后续版本，但必须先通过带 hard negative 的评测。

**怎么避免图谱幻觉？**

抽取结果先进入 pending candidate，审核后才成为 active 图事实；读取时关系必须携带来源 Chunk，
adapter 和 toolkit 各做一次 scope/evidence 校验；最后 publisher 又要求 citation ID 来自本轮 allowlist。
系统不能证明“抽取一定正确”，但能保证每条被使用的图事实有来源、状态和可撤销路径。

## 不应夸大的表述

- 不写“ChatTutor/Desktop-Claw 有的功能我全都有”；任务/计划/笔记、Persona/day archive、桌面分发、
  公共 API 鉴权和交互 Run 恢复仍在路线图，详见 `docs/REFERENCE_PROJECT_COMPARISON.md`。
- 不写“知识图谱保证事实正确”；应写“证据绑定、审核门禁、可追溯和可撤销”。
- 不写“实现通用实体链接 SOTA”；当前是确定性 canonical/alias resolver。
- 不写“支持任意深度图推理”；在线严格限制为 1-3 hop。
- 不写“528 篇实体关系已经全部抽取完成”。当前 25 个低文本页已补齐，528 篇均完成 PDF 校验、
  11,023 页文本/Vision OCR、Document IR、43,872 chunks 和 Neo4j 结构投影；旧语义候选已因证据 revision
  失效而归档，active 语义实体/关系仍为 0。v6 只通过了 5-case/18-case 质量门禁，全量 backfill 待
  共享模型网关恢复稳定后执行。

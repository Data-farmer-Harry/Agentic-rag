# HermesGraph Engineering Intelligence Agent

技术实现设计文档

版本：v1.6

日期：2026-08-03

## 0. 实现原则

这份文档是 Hermes-first、OpenAI-powered 自进化多模态 Engineering Intelligence Agent 的底层工程
基线。研发团队是默认业务主线，个人学习由同一 Agent/RAG/Graph/Memory 内核支持。当前体验交付、
软件工程 DomainPack、模拟企业语料和阶段门禁以
`docs/ENGINEERING_INTELLIGENCE_AGENT_DELIVERY.md` 为准；本文继续定义不会被前端产品调整破坏的
架构、数据、运行时和治理合同。每个阶段都定义目标、接口、数据结构、测试和完成条件。当前应用镜像
与 Hermes sidecar 使用 Python 3.13，依赖通过 lock file 或精确版本固定；不在源码中隐式依赖
`latest`，升级必须经过 contract、eval 和真实纵向验收。

### 0.1 选定技术边界

```text
OpenAI Python SDK      Responses、Structured Outputs、Vision、Embeddings、hosted Web Search、Background API
Hermes Agent 0.19.0    唯一在线 Agent runtime、tool loop、session、Todo、原生 Memory/Skill、后台回顾
LangChain              系统衔接层：Runnable 数据流、loader、splitter、retriever、Prompt、结构化转换、工具适配、callbacks
HermesGraph Bridge     run-scoped capability、预算、scope、证据白名单、严格答案发布、原生学习审计/回滚
Neo4j                  知识图谱、实体关系、路径查询、图谱向量补充索引
Qdrant                 dense/sparse hybrid retrieval、filter、fusion、rerank candidate store
PostgreSQL             租户、任务、文档元数据、评测、技能版本、审计、幂等状态
File/S3 object store    文本、图片、音视频原件、视觉派生物、快照和导出物
Postgres job/outbox     当前 durable background control plane；需要时再接外部 orchestrator
OpenTelemetry           服务级 metrics/logs/traces；Agent trace 通过适配器关联
FastAPI                 HTTP/SSE API
```

Responses API 是文本、视觉和结构化模型调用的首选原语。当前在线请求由固定版本的 Hermes Agent sidecar 维护唯一 Tool Calling Loop；`HermesAgentRuntime` 通过 HTTP/SSE 适配到现有 `AgentRuntime` 合同。Hermes 的受信任插件只调用 HermesGraph Capability Bridge，领域合同、Knowledge Object 和 Learning Control Plane 不依赖 sidecar 内部实现。LangChain 是贯穿系统的 Integration Runtime，负责把 loader、splitter、retriever、图谱、Prompt、结构化转换、能力工具和 callbacks 组合成可测试的 Runnable 数据流；它可以执行确定性分支、并行和重试，但不得用 `create_agent`、LangGraph 或 middleware 再创建一个处理同一用户请求的 Agent Loop。

### 0.2 OpenAI 原语职责

| 原语 | 技术职责 | 失败语义 |
| --- | --- | --- |
| Responses API | 文本/视觉理解、结构化抽取、hosted Web Search、需要时的长响应 | timeout/refusal/incomplete 均进入受控失败，不发布半成品 |
| Tool Calling | 公开少量稳定能力给在线 Agent | schema、scope、effect、预算和重复调用门禁 |
| Structured Outputs | 计划、实体关系、视觉描述、证据判断、学习候选 | Pydantic `extra=forbid`，应用层再次校验证据 ID |
| Vision | 图片、截图、扫描页、图表和 PDF 页面 | 原图保留；输出绑定媒体 hash、page/region 和 model revision |
| Embeddings | 文本及视觉派生语义向量 | revision/维度隔离，不能混写 collection |
| Background Tasks | 长时模型任务，仅在收益明确时使用 | 本地 job ID 与 provider response ID 映射，可取消、超时和恢复 |
| Hermes Agent | 唯一在线会话、工具编排、原生 Memory/Skill 和后台回顾 | 固定版本、单根循环、bridge fail-closed、禁止未批准工具集 |

### 0.3 架构不变量

1. 在线请求只有一个 Agent Loop，由 Hermes Agent `0.19.0` sidecar 管理。项目不安装或装配第二个 Agent SDK runtime。
2. LangChain 是能力衔接层，不被缩减为单一 RAG helper，也不拥有最终会话控制权。
3. 产品默认是研发团队工作区，同时支持个人私有工作区；tenant/project/user 作用域仍在 repository、
   vector、graph、event 层强制。
4. 核心运行时领域无关；`software_engineering` 是第一个业务 DomainPack，个人通用知识和其他专业
   schema、检索模板与评测继续通过 `DomainPack` 注入。
5. 自进化分为两条明确通道：Hermes 原生 Memory/Skill 先应用后审计，但应用前必须保存精确快照；
   HermesGraph 高影响资产先评测后晋级。
6. Hermes 原生学习不能覆盖 HermesGraph active 图谱事实、Prompt、安全策略、外部权限或核心代码；这些只能形成候选资产。
7. 任意事实、记忆、视觉描述和图关系必须保留 provenance；没有来源的内容不能升级为高信任知识。
8. 原始多模态对象不可被模型摘要替代；派生产物必须绑定源对象 hash、区域/时间码和 extractor revision。
9. 系统必须支持无外部数据库、无模型密钥的离线测试模式，并明确标记其质量边界。
10. 公共网络检索与个人知识检索使用不同 scope 和 trust；无 URL citation 的 Web 输出不得进入
    answer allowlist，网络证据不能仅凭来源链接被标成 verified。
11. 最终答案必须通过 `hermesgraph_publish_answer`，且只引用本轮 Capability Bridge 返回的
    evidence ID；Hermes 自然语言终态不能绕过应用发布门禁。

### 0.4 复杂度策略

系统内部可以很复杂，但对 Agent 暴露的工具必须少而稳定。当前在线运行时按部署能力暴露以下
bridge read/publish tools 与 Hermes 原生学习工具：

```text
search_knowledge               个人/项目/已摄取公共资料的 Agentic Retrieval Controller
resolve_graph_entities         canonical/alias/type 实体解析与来源证据
retrieve_evidence_subgraph     文本检索与 1-3 hop 证据子图融合
compare_graph_entities         连接路径、共享邻居和左右独有邻居
search_graph                   固定模板邻居、路径和冲突遍历
search_web                     当前或知识库外公共事实的受控 Responses hosted search（可选）
recall_project_memory          HermesGraph 受治理项目记忆
hermesgraph_publish_answer     严格回答 draft 与 evidence ID 发布
memory / skill_manage / todo   Hermes 原生个人学习与任务工具
```

Hermes 可写自己的持久 profile Memory/Skill，但插件必须在原生写工具执行前保存受限快照、执行后计算
内容哈希，再把成功写入镜像为待审计 ChangeSet；它不能直接写 HermesGraph repository、Neo4j、
Qdrant 或安全策略。审计日志只保存学习参数的长度和 SHA-256，不复制 Memory/Skill 正文。高影响学习
仍由可信 `run.completed` 事件触发 Learning Control Plane 生成候选。Agent 不直接看到 Neo4j driver、
原始 Cypher、Qdrant collection 或 Postgres SQL。bridge 能力全部经过权限、运行作用域、租户过滤、
参数校验、预算、超时和审计。

## 1. 总体架构

### 1.1 逻辑架构

```mermaid
flowchart TB
    U["Personal Workbench / API"] --> API["FastAPI + SSE"]
    API --> RUN["HermesAgentRuntime adapter"]
    RUN --> H["Hermes Agent 0.19.0 sidecar\nOnly online agent loop"]
    H --> RESP["OpenAI-compatible model gateway"]
    H --> PLUG["Trusted hermesgraph-bridge plugin"]
    PLUG --> BRIDGE["Authenticated run-scoped\nCapability Bridge"]
    BRIDGE --> CAP["Capability Registry"]
    CAP --> RET["Agentic Retrieval Controller\nLangChain Integration Runtime"]
    CAP --> WEB["Controlled Web Search\nweb:read + citation normalization"]
    WEB --> RESP
    RET --> T1["Hybrid / metadata search"]
    RET --> T2["Graph paths"]
    RET --> T3["Evidence fetch / verify"]
    RET --> Q["Qdrant"]
    RET --> N["Neo4j"]
    SRC["Personal files / images / screenshots"] --> JOB["Postgres ingestion jobs"]
    ARX["arXiv metadata + selected PDFs"] --> SYNC["Rate-limited source sync"]
    SYNC --> JOB
    JOB --> ING["Durable ingestion worker"]
    ING --> OBJ["File / S3 object store"]
    ING --> PARSE["Text / PDF parsing"]
    ING --> VISION["Vision structured extraction"]
    VISION --> RESP
    PARSE --> PG["PostgreSQL metadata + outbox"]
    VISION --> PG
    PARSE --> Q
    VISION --> Q
    PARSE --> N
    VISION --> N
    MEM --> PG
    MEM --> Q
    EVT["run.completed + feedback"] --> LEARN["Learning Control Plane"]
    LEARN --> MEM
    LEARN --> SK["Versioned Skill Registry"]
    SK --> PG
    H --> NATIVE["Hermes native Memory / Skills / Todo"]
    NATIVE --> SNAP["Pre-write exact snapshot\nPost-write content hash"]
    SNAP --> HAUDIT["Sanitized native audit\nrequires_audit ChangeSet"]
    API --> REVIEW["Native learning review API"]
    REVIEW --> ADMIN["Internal rollback admin :8643"]
    ADMIN --> SNAP
    RUN --> TRACE["Run trajectory + evidence"]
    TRACE --> OBS["Metrics / eval / replay"]
```

### 1.2 服务边界

| 服务 | 职责 | 不负责 |
| --- | --- | --- |
| API | 鉴权、会话、SSE、任务提交 | 直接访问数据库 |
| Hermes Agent sidecar | 唯一在线循环、会话、原生 Memory/Skill、Todo、后台回顾 | 直接访问业务数据库、自由 Shell、最终 citation hydration |
| Agent Runtime adapter | 创建/停止 Hermes run、消费 SSE、状态轮询、取消和超时 | 文档解析、数据库查询细节 |
| Capability Bridge | 认证、run scope、工具预算、证据 allowlist、严格答案发布、原生写入审计 | Agent 决策、数据库自由查询 |
| Native Learning Control | 派生待审计状态、接受/回滚决策、append-only review ledger | 修改原 ChangeSet、绕过 after-hash 前置条件 |
| Integration Runtime | query rewrite、检索组合、Prompt、结构化转换、工具适配、callbacks | 在线 Agent Loop、最终会话控制 |
| Graph | entity linking、Cypher 模板、路径和子图 | 任意 Cypher 执行 |
| Document | 原文、片段、页码、表格、版本 | Agent 决策 |
| Media/Vision | 媒体校验、标准化派生图、区域、OCR/描述、模型 revision | 用摘要替代或删除原始媒体 |
| Source Sync | arXiv/个人来源发现、游标、速率与下载预算 | 无上限爬取、绕过来源条款 |
| Web Search | 当前公开事实、URL citation、domain policy、敏感查询阻断 | 私有知识搜索、网页指令执行、无引用摘要发布 |
| Evidence | claim 提取、引用匹配、支持/冲突判断 | 修改原文 |
| Learning Control Plane | trace 评估、候选记忆、技能候选、回归、晋级 | 无门禁发布、在线会话控制 |
| Capability Registry | 统一输入输出、权限、预算、版本、错误和 provenance | 底层业务实现、Agent 决策 |
| Ingestion | 异步解析和索引 | 在线回答 |

### 1.3 运行模式

- `sync`: 简单问答，HTTP 请求内完成，建议最大 8 个工具回合。
- `stream`: 通过 SSE 返回阶段事件，适合交互式研究问答。
- `async`: 复杂综述或批处理，任务写入队列，客户端轮询或订阅事件。
- `replay`: 使用历史 trace 和固定语料重放，不访问真实写入工具。
- `shadow`: 学习资产参与决策但不改变最终策略，用于评估收益。

### 1.4 工作台与流式协议

本地与单机部署采用同源架构：FastAPI 同时提供 `/v1/*` API、OpenAPI 和 `frontend/dist` 静态产物。前端不得直接读取 `.data`、Neo4j、Qdrant 或 repository 文件；运行历史、Memory、Skill、LearningChangeSet、Capability 和 Graph 均通过带 `project_id` 的 scoped API 访问。

知识库同样只使用 scoped API。Compose 默认通过 `POST /v1/projects/{project_id}/ingestion-jobs` 提交 durable 异步任务，使用 `GET` 列表/详情、`DELETE` 取消和 `POST .../{job_id}/retry` 人工重试；文档 `GET` 列表/详情与 `DELETE` 逻辑归档保持不变。旧 `POST .../documents` 同步入口仍用于降级和兼容。multipart 入口只读取 `max_upload_bytes + 1` 字节并在解析前拒绝超限内容，避免无界内存占用。

流式任务使用两段式协议：`POST /v1/projects/{project_id}/runs/start` 接收输入、session、user、
DomainPack 和客户端生成的 `idempotency_key`，先返回稳定 `run_id`；随后
`GET /v1/projects/{project_id}/runs/{run_id}/events/stream?after_cursor=N` 以
`text/event-stream` 观察运行。旧 `POST .../runs/stream` 保留为兼容入口，但内部同样委托给
`RunStreamCoordinator`。前端使用 `fetch` 的 `ReadableStream` 解析 SSE `id`、event 和 data。

会话是工作台的一等用户对象，但不另建一份聊天真相源：

- `GET /v1/projects/{project_id}/conversations` 从 scoped trajectory 聚合 session 标题、最新预览、
  轮数、状态和时间；`include_archived=true` 可返回归档会话。标题取首轮用户可见输入，内部附件
  标记不会进入标题或预览。
- `GET /v1/projects/{project_id}/conversations/{session_id}/runs` 按时间正序恢复完整消息。
- `PATCH /v1/projects/{project_id}/conversations/{session_id}` 持久化自定义标题和逻辑归档状态。
  元数据按 tenant/project/user/session 隔离；归档不删除 trajectory，恢复后仍可读取完整上下文。
- 前端只在 `localStorage` 保存当前 session ID 和未发送草稿；已发送内容以服务端 trajectory 为准。
- 命令面板只搜索已经通过 scoped API 加载的 active conversation summary，包括标题、最新预览和
  更新时间。选择结果仍调用 session run 恢复 API；搜索索引不是第二份会话存储，也不包含归档内容。
- “新建对话”必须生成新的不可预测 session ID，不能只清空 React 状态。
- 聊天附件复用现有同步或 durable ingestion API。异步模式轮询单个 job，只有 `succeeded` 文件可
  随消息发送；最多 5 个，失败文件必须移除或重新选择。用户输入末尾使用可解析的
  `<attachments>` 块保存文件名，前端恢复时将其渲染为附件标签而不是普通正文。
- `POST /v1/projects/{project_id}/memories` 提供用户显式记忆入口。内容按 scope/type/hash 幂等，
  provenance 固定为 `user_explicit/user_asserted`，不会伪装成模型推断。
- `run.error` 只返回稳定错误码、可操作消息和 `retryable`，不得把 provider URL、凭据状态或原始
  异常直接显示给用户。
- `RunStreamCoordinator` 而不是 HTTP generator 拥有后台执行 task。启动时先持久化带幂等键的 running
  trajectory，再生成 `run.accepted`；同 tenant/project/user 下重复幂等键返回同一 run，不再次执行。
- `RunEventRecorder` 将 stream event 追加到 `run_events.jsonl`，每个 run 使用从 1 开始单调递增的
  cursor，同时向进程内订阅者广播。重连先在注册订阅者的同一锁内读取 `cursor > after_cursor` 的
  backlog，再消费新事件，避免读取与订阅之间漏事件；tenant/project/user scope 不匹配返回 404。
- 浏览器连接结束只解除观察订阅，不取消后台 task。`DELETE /v1/projects/{project_id}/runs/{run_id}`
  是唯一用户取消入口，等待 `RunService` 写入 cancelled trajectory 和 `run.cancelled` 终态事件。
- 首次使用引导直接读写 Personal Control Plane 的版本化 `PersonaProfile`，不得创建独立用户档案。
  保存时提交 `expected_version` 和 `complete_onboarding=true`；“稍后”只在本机记录 24 小时的展示
  冷却，不改变服务端 Persona，也不阻塞聊天。

事件合同：

```text
run.accepted
  -> run.status
  -> run.heartbeat*
  -> tool.completed*
  -> answer.delta*
  -> evidence.added*
  -> learning.updated?
  -> run.completed | run.cancelled | run.error
```

- `run.heartbeat` 保证长任务期间代理、浏览器和用户能观察连接活性；前端使用 `elapsed_ms` 展示
  已运行时间，完成后保留 duration、工具调用数和学习更新数，不展示私有推理。
- `tool.completed` 在工具完成后立即投影到本轮订阅者，并在 `run.completed` 前补发任何尚未投影的
  trajectory tool event。前端只把 allowlisted 名称映射为用户可读活动；不显示 input hash、参数、
  原始 output summary 或模型思维。重复投影使用工具名、input hash 和创建时间去重。
- `run.error` 额外包含失败阶段与服务端 duration。客户端保留 `retryable`：可重试失败重放原用户
  请求，不可重试错误提示检查配置；用户主动 Abort 映射为 cancelled UI，而不是 provider failure。
- `answer.delta` 只传输最终可公开答案，不暴露模型私有推理内容。
- `evidence.added` 只能来自服务端已经通过 publisher gate 的本轮 citation。
- `learning.updated` 只报告后台候选变更，不表示 Skill 已晋级或 Memory 已变为高信任事实。
- 前端在 session 恢复时若发现最新 trajectory 仍为 running，会从 cursor 0 重建活动状态并继续订阅；
  单次连接内重试只请求上次确认 cursor 之后的事件。工具和 evidence 仍按稳定键去重，`run.completed`
  使用服务端最终 answer 覆盖增量文本。
- 当前保障的是浏览器刷新、网络抖动和 HTTP 连接重建，不是跨 App 进程恢复任意模型 coroutine。进程
  重启后若发现没有 owner 的 running trajectory，会确定性写为 failed、tag `run_interrupted` 并发布
  可重试 `run.error`，避免假装任务仍在运行。真正跨进程继续执行仍需把 Agent run 提升为 durable job。
- 当前 API 在完成 HermesGraph 发布门禁后分块发送答案；后续可把 Hermes 已验证的公开 final-output delta 映射为更细粒度 SSE，但不得流出私有推理或绕过最终 publisher 校验。

### 1.5 多模态 Knowledge Object 合同

当前过渡实现继续复用 `KnowledgeDocument` 和 document/chunk API，但已经把图片作为一等入库
对象：原始 bytes 由 content endpoint 回读，文档 metadata 标记 `modality=image`，Vision 总览与
区域作为可检索 chunk 保存，区域 locator 贯通 Qdrant、Neo4j structural projection 和 citation。
这使首条多模态链路在不破坏现有 API 的前提下交付。正式对象存储迁移仍将引入
`KnowledgeObject` 和 `DerivedArtifact`，将原件与派生物拆成独立表和生命周期：

已交付的过渡合同 `KnowledgeSource` 固定包含 `source_type/source_id/title/source_revision/`
`canonical_uri/license_uri/privacy/trust/acquired_at`。它随 `IngestionJob` 进入 Postgres migration
v3，并随 `KnowledgeDocument` 原子持久化；Qdrant payload、Neo4j Document/Chunk 和最终
`EvidenceRef` 只能从该合同投影，不能各自硬编码来源。当前图片合同已实现原图 hash、Vision
revision、可见文字、视觉类别、region ID 与归一化 bounding box；下述独立对象表仍是生产目标。

当前 `OpenAIVisionAnalyzer` 已升级到 `openai-vision-knowledge-v3:<model>`：图像始终是
untrusted evidence；图内指令只能进入 OCR、区域证据和 warning，不能成为系统指令或污染摘要。
主要图、表、代码、界面或笔记各自形成自包含区域，普通论文段落只进入顶层 visible text，近空白
图片返回零区域。Responses 的 incomplete、refusal 或无 parsed output 失败关闭；生产异步入库由
durable job 重试，评测器只对明确的传输/限流/服务端错误执行有记录的样本级恢复。

```text
KnowledgeObject
  object_id, tenant_id, project_id, user_id
  object_kind: document | image | pdf_page | audio | video
  media_type, byte_size, content_hash, storage_key
  source_type, source_uri, source_revision, license_uri
  privacy: private | public_reference
  status, created_at, updated_at

DerivedArtifact
  artifact_id, object_id, artifact_kind
  text, content_hash, storage_key?
  page_number?, region?, time_start_ms?, time_end_ms?
  extractor_provider, extractor_model, extractor_revision
  source_object_hash, status, created_at

VisualRegion
  page_number?, x, y, width, height  # 0..1 normalized coordinates
  label, visible_text?, confidence
```

约束：

1. 原件先以 content hash 和受控 storage key 持久化，再创建派生任务；数据库失败可能留下可
   垃圾回收的 orphan object，但不能出现指向不存在原件的 active metadata。
2. 图片 MIME、magic bytes、像素、帧数和解压后尺寸均有上限；标准化派生图移除不需要发送给
   模型的 EXIF，原件仍按用户保留策略保存。
3. Vision 使用 Responses API Structured Outputs。模型正文只产生 pending artifact/candidate；
   应用再次验证 region、page 和 object ID 属于当前 batch。
4. PDF 先做文本解析；扫描页、图表页或用户明确要求的页面再渲染成受限图片进入 Vision，避免
   对每页无差别付费。所有视觉结论保留 page/region。
5. Qdrant point 必须记录 object/artifact ID、modality、model revision、scope、status 和 hash。
   文本 query 可以召回视觉描述，但 citation 必须回到原始图片或 PDF 页。
6. Neo4j 只接收 pending 候选投影；批准语义关系前仍经过既有 `GraphCandidateService`。
7. 归档 Knowledge Object 要同步隔离其全部派生物、向量、图节点和视觉引用；审计记录保留。

### 1.6 arXiv 计算机语料同步

arXiv 同步器不是页面爬虫，而是一个可恢复 Source Connector：

首版已经在 `app/sources/arxiv.py` 和 `app/sources/arxiv_cli.py` 落地；OAI-PMH 定时游标和
corpus 级 bulk adapter 尚未实现。

```text
topic/category config
  -> arXiv Atom API metadata pages
  -> normalize arXiv_id + version + updated + categories + license
  -> relevance/dedup filter
  -> durable download job
  -> PDF object + metadata sidecar
  -> normal ingestion / Vision-on-selected-pages
  -> Qdrant + pending graph candidates
```

- 小规模主题检索使用官方 Atom API；大规模元数据镜像改用官方推荐的 OAI-PMH。完整语料不能
  通过主站逐页抓取；若未来确需 corpus 级全量，使用官方 S3/Kaggle bulk channel。
- 下载只访问专用机器接口或 metadata 返回的 canonical PDF link，设置明确 User-Agent、请求
  超时、指数退避、`Retry-After`、全局速率和每日 byte/paper budget。
- 初始配置聚焦 `cs.AI/cs.CL/cs.IR/cs.LG/cs.CV/cs.SE/cs.HC`，并以 Agentic RAG、knowledge
  graph、long-term memory、tool use、multimodal agent、self-improving/evolving agent 为主题。
- `source_id=arxiv:{id}`、`source_revision=v{version}`，保存规范标题、abstract URL、PDF URL、作者、
  分类、published/updated、DOI/journal ref、license URI 和抓取时间。新版本创建新 source revision，
  不覆盖旧证据。来源/索引契约升级时通过本地 manifest 幂等 resubmit，不重新下载 PDF。
- arXiv 论文是 `public_reference`，产品展示回链，不把本地缓存作为再分发端点；个人知识仍是
  `private`，二者的删除、导出和信任策略分离。
- 本地提交分为显式重建和可续传增量两种语义：`--refresh-submitted` 有意重提所有缓存版本；
  `--submit-pending` 不访问 arXiv，只提交存在有效 PDF 且没有 `ingestion_job_id` 的记录，重复执行
  不会制造第二个 job。
- PDF 文本化支持完全离线的 `--text-only`。有效文本层直接写入按页 Markdown，低文本页标记为
  `unresolved_low_text`，不构造模型 client、不渲染页面，也不伪造 OCR 成功；恢复 Vision 模式后，
  processor 只重新处理含待补页的文档，其他文档继续按 source hash 与输出路径跳过。
- OCR sidecar 同时保存结构化 `DocumentIR`：`PageLayer -> Block -> Section` 均带稳定 ID、页码、
  extraction method、confidence、warning 和原文 locator。当前 528/528 篇使用
  `document-ir-pdf-v1`，11,023 页中 10,995 页使用 PDF 文本层、28 页使用 Vision OCR，
  `unresolved_low_text=0`；共 168,531 blocks，其中 167,487 native text、1,044 Vision OCR。IR 与面向人工检查的按页
  Markdown 共存，后续 chunking 不再从拼接字符串反推版面结构。
- 2026-07-22 受控快照为 777 个候选版本、528 个唯一 PDF、`1,059,247,539` 字节；全量重读验证
  path containment、文件存在、byte size、`%PDF-` magic 和 SHA-256，六项异常均为 0。新同步仍以
  10 MB 为单篇上限，入库上传上限显式设为 20 MB，只用于兼容 6 篇早期 10-13.3 MB 缓存。
- 官方说明 OAI-PMH 是批量元数据和每日增量的首选；官方同时警告不要程序化下载完整语料，
  corpus 级下载应使用其 S3 通道。实现与运维必须遵守这些边界。

## 2. 代码仓库结构

```text
hermesgraph/
├── pyproject.toml
├── requirements.lock
├── requirements.runtime.lock
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── deploy/hermes/
│   ├── Dockerfile
│   ├── entrypoint.sh
│   ├── bootstrap.py
│   ├── config.yaml
│   └── plugin/
│       ├── plugin.yaml
│       ├── __init__.py
│       └── native_snapshots.py
├── app/
│   ├── main.py
│   ├── config.py
│   ├── bootstrap.py
│   ├── worker.py
│   ├── api/app.py
│   ├── application/
│   │   ├── run_service.py
│   │   └── workspace_service.py
│   ├── domain/{models,enums,contracts}.py
│   ├── agent/
│   │   ├── hermes_runtime.py
│   │   ├── hermes_bridge.py
│   │   ├── hermes_native_learning.py
│   │   ├── offline_runtime.py
│   │   ├── instructions.py
│   │   ├── model_provider.py
│   │   └── budget.py
│   ├── integration/
│   │   ├── runtime.py
│   │   ├── capability.py
│   │   ├── callbacks.py
│   │   └── adapters.py
│   ├── retrieval/
│   │   ├── agentic.py
│   │   ├── pipeline.py
│   │   ├── qdrant_hybrid.py
│   │   └── embeddings.py
│   ├── graph/{neo4j,local,extraction,structured_extraction,resolution}.py
│   ├── knowledge/{ingestion,jobs,store,retriever,provenance}.py
│   ├── evidence/publisher.py
│   ├── memory/{write_gate,prompt_capsule,json_store}.py
│   ├── skills/{registry,repository,rollout,parser}.py
│   ├── learning/
│   │   ├── engine.py
│   │   ├── jobs.py
│   │   ├── reflection.py
│   │   ├── evolution.py
│   │   ├── refinement.py
│   │   ├── skill_replay.py
│   │   └── promotion.py
│   ├── infra/{postgres*,local_repositories,outbox_*}.py
│   ├── vision/openai_analyzer.py
│   ├── web_search/openai_hosted.py
│   ├── sources/{arxiv,arxiv_ocr}.py
│   └── evaluation/
├── prompts/
│   ├── hermes_runtime.md
│   └── ...
├── frontend/
├── examples/evaluation/
├── scripts/
└── tests/
    ├── unit/
    ├── contract/
    └── integration/
```

## 3. 配置与依赖

### 3.1 初始安装

本地应用代码要求 Python 3.11+；当前 Docker 镜像使用 Python 3.13。仓库已有
`requirements.lock`、`requirements.runtime.lock` 与 `pyproject.toml`，不要按文档手工拼接一套
不同依赖。应用开发环境按以下命令建立：

```bash
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
npm --prefix frontend ci
npm --prefix frontend run build
```

Hermes 不安装进应用虚拟环境，而是由 `deploy/hermes/Dockerfile` 构建独立 sidecar。镜像固定
`hermes-agent==0.19.0`、`aiohttp==3.14.1` 和基础镜像 digest，以非 root 用户运行。完整本地栈使用：

```bash
./scripts/docker_up.sh
docker compose ps
```

当前运行时不依赖 Redis 或 MinIO；durable ingestion/learning/outbox 使用 Postgres，原始对象当前写入
`app_data`，S3 兼容对象存储属于后续生产扩展。Neo4j 查询由项目 adapter 的固定模板实现，不要求
应用直接依赖 `neo4j-graphrag`。

### 3.2 `.env.example`

```dotenv
APP_ENV=local
APP_NAME=hermesgraph
DATA_DIR=.data

RUNTIME_MODE=hermes
HERMES_API_URL=http://127.0.0.1:8642
HERMES_API_KEY=replace-with-at-least-32-random-characters
HERMES_BRIDGE_TOKEN=replace-with-a-different-32-character-secret
HERMES_NATIVE_ADMIN_URL=http://127.0.0.1:8643
HERMES_NATIVE_ADMIN_TOKEN=replace-with-a-third-independent-32-character-secret
HERMES_NATIVE_ADMIN_TIMEOUT_SECONDS=10
HERMES_NATIVE_SNAPSHOT_MAX_BYTES=5000000
HERMES_NATIVE_SNAPSHOT_MAX_TOTAL_BYTES=1000000000
HERMES_NATIVE_SNAPSHOT_RETENTION_DAYS=30
HERMES_NATIVE_SNAPSHOT_TERMINAL_RETENTION_DAYS=7
HERMES_NATIVE_SNAPSHOT_NO_CHANGE_RETENTION_HOURS=24
HERMES_POLL_INTERVAL_SECONDS=0.25

OPENAI_MODEL=gpt-5.6
OPENAI_API_KEY=
MODEL_PROVIDER=openai
MODEL_BASE_URL=
DOCKER_MODEL_BASE_URL=
MODEL_API_KEY=

POSTGRES_DSN=postgresql://hermesgraph:hermesgraph-dev@127.0.0.1:5432/hermesgraph
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=hermesgraph_chunks_v3_idf
QDRANT_SPARSE_IDF=true
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=hermesgraph-dev

MAX_AGENT_TURNS=10
MAX_TOOL_CALLS=20
MAX_RETRIEVAL_TOOL_CALLS=3
MAX_GRAPH_TOOL_CALLS=6
MAX_WEB_SEARCH_TOOL_CALLS=3
AGENT_TIMEOUT_SECONDS=90
CONVERSATION_FAST_PATH_ENABLED=true
CONVERSATION_FAST_PATH_TIMEOUT_SECONDS=20

RETRIEVAL_BACKEND=qdrant
EMBEDDING_PROVIDER=deterministic
EMBEDDING_DIMENSIONS=256
GRAPH_BACKEND=neo4j
GRAPH_EXTRACTOR_MODE=rule
AGENTIC_RETRIEVAL_ENABLED=true
RETRIEVAL_PLANNER_MODE=deterministic

WEB_SEARCH_MODE=disabled
WEB_SEARCH_MODEL=gpt-5.6
WEB_SEARCH_CONTEXT_SIZE=medium
WEB_SEARCH_MAX_RESULTS=8
WEB_SEARCH_ALLOWED_DOMAINS=[]

INGESTION_MODE=async
KNOWLEDGE_REPOSITORY_BACKEND=postgres
LEARNING_JOB_MODE=async
LEARNING_ARTIFACT_BACKEND=postgres
LEARNING_MODE=shadow
```

`RUNTIME_MODE` 只有两个合法值：`hermes` 是唯一在线运行时；`offline` 是无模型 deterministic
测试与演示。`openai` 已从配置 schema 删除，不能静默恢复旧 fallback。生产和预发布选择 `hermes` 时，Hermes API、
Hermes -> HermesGraph bridge、HermesGraph -> Hermes native admin 三个 secret 必须分别至少 32 字符且
不能复用。Compose 会把应用的 `HERMES_API_URL` 改为 `http://hermes:8642`，把 native admin URL
改为 `http://hermes:8643`；`8643` 不发布到宿主机。Hermes sidecar 优先使用 `MODEL_API_KEY`，未设置
时回退 `OPENAI_API_KEY`；
模型 base URL 优先使用 `DOCKER_MODEL_BASE_URL`，未设置时回退 `https://api.openai.com/v1`。本机兼容
网关必须显式配置容器可达的 `DOCKER_MODEL_BASE_URL`，不能把宿主 `127.0.0.1` 原样传进容器。

### 3.2.1 对话、Agent 与 RAG 的执行分流

在线请求不是全部进入 RAG。`ConversationRoutedRuntime` 在 Hermes 之前执行三层保守路由：

1. **确定性社交快速通道**：完整匹配的短问候、感谢和告别直接生成
   `response_mode=conversational`；不调用模型、Hermes、Qdrant、Neo4j 或发布工具。确认词只有在
   当前 session 没有可用历史时才能走这条通道；已有历史的“好的/继续”必须进入上下文判断，避免
   丢失上一轮动作或事实任务。
2. **通用轻量对话通道**：`domain_pack=general` 的其他请求先调用一次无知识工具的轻量
   Chat Completions。它只能直接回答闲聊、情绪陪伴和非事实性创作，并且只暴露
   `delegate_to_agent` 一个升级工具。事实、时效、专业知识、个人记忆、文件、引用、行动、持久化和
   上下文不足的请求必须调用该工具；空响应、超时或 provider 错误也 fail-safe 升级到 Hermes。
3. **Hermes Agent 通道**：研究/技术文档领域包直接进入，通用通道升级的请求也进入。进入 Hermes
   不等于无条件检索；Hermes 根据任务选择 knowledge、graph、web、workspace、memory 或 personal
   工具。只有需要证据的回答使用 `response_mode=grounded` 和 claim/citation 门禁。

因此，RAG 是按需能力而不是聊天入口。明确社交消息的目标延迟是本地亚秒级，普通模型闲聊目标是一次
模型调用，专业研究任务允许多轮 Agent/tool 延迟。`CONVERSATION_FAST_PATH_ENABLED=false` 可关闭
前两层；轻量模型调用由 `CONVERSATION_FAST_PATH_TIMEOUT_SECONDS` 限时，并通过
`CONVERSATION_FAST_PATH_MODEL` 与 Hermes 主模型独立配置。当前部署使用 `gpt-5.6-luna` 处理快速
通道，Hermes 继续使用 `gpt-5.6-sol`。

轻量通道读取 `TrajectoryRepository.list_session()`，严格按 tenant/project/user/session 隔离，
只选择已经完成且有答案的运行，排除当前 running run。默认最多 8 轮、12,000 字符，并分别由
`CONVERSATION_HISTORY_TURNS`、`CONVERSATION_HISTORY_MAX_CHARS` 限制。历史消息仍视为 untrusted
data，不能覆盖系统路由规则。最终答案持久化 `routing_lane=deterministic|conversation|agent`；
workspace overview 汇总各通道数量。`hermesgraph-eval-conversation-routing` 使用版本化黄金集并发
评测 pass rate、P95、混淆矩阵、危险直答和过度升级；只要 Agent 期望用例被直接回答，门禁即失败。
运行快照只有携带 `component_versions.conversation_router=2` 才进入分流统计；升级前的历史回答在
overview 中单列为 `legacy`，即使旧版兼容默认值曾序列化为 `agent`，也不能伪装成 Agent 路由样本。

`run.completed` 的纯 conversational 回合不进入自动 reflection/mining，避免把“你好”或普通闲聊
写成 Memory/Skill；用户显式反馈仍触发 `feedback_received` 学习。Hermes 调用
`hermesgraph_publish_answer` 后，bridge 的 publication event 是运行硬终态：应用停止 sidecar run 并
立即返回已验证答案，不再等待无意义的发布后模型回合。

`WEB_SEARCH_MODE` 默认 `disabled`。设为 `openai` 时，官方 provider 需要
`OPENAI_API_KEY`，兼容 provider 需要 `MODEL_BASE_URL/MODEL_API_KEY`，并且必须先运行
`scripts/check_web_search.py` 验证 Responses `web_search` 与 URL annotation。domain allowlist
是 JSON 数组，只接受 bare DNS domain；配置会发送给 provider，返回结果仍由应用层再次检查。

### 3.3 配置分层

运行配置分为 `static`、`tenant`、`project`、`user`、`task` 五层。低层只能收窄权限，不能绕过高层安全策略。模型、工具、检索权重和技能版本都必须写入任务快照，使历史任务可复现。

## 4. 领域数据模型

### 4.1 统一 ID 与版本

- 所有实体使用 UUIDv7 或 ULID，按时间有序。
- 文档内容用 `sha256` 计算 `content_hash`。
- 向量索引用 `embedding_model`、`embedding_revision` 标识。
- 图谱抽取用 `extractor_version` 标识。
- 用户可见引用必须包含 `document_id`、`document_version_id`、`chunk_id`。
- 任何可学习资产必须包含 `asset_version`、`parent_version`、`created_by`、`promotion_status`。

### 4.1.1 Capability Contract

每个跨框架能力必须声明 `name`、`version`、输入/输出 JSON Schema、read/write effect、required scopes、timeout、retry owner、幂等性、最大输出大小、敏感字段和 provenance 规则。OpenAI tool 与 LangChain Runnable 都只能适配该合同，不能直接互相依赖框架私有类型。

### 4.1.2 RunSnapshot

每次运行固定模型与参数、Prompt hash、DomainPack 版本、Skill/Policy 版本、语料发布版本、embedding/reranker/extractor 版本、Capability 版本和工具结果 hash。Replay 复现领域合同和工具结果，不承诺模型自然语言逐字一致。

### 4.1.3 LearningChangeSet

每次学习变更包含目标资产、父版本、结构化 diff、来源 run、预期收益、风险、作用范围、评测报告、审批记录、曝光比例和回滚条件。没有 ChangeSet 的候选不能进入 shadow。

### 4.2 PostgreSQL 表

```sql
create table tenants (
  id uuid primary key,
  name text not null,
  created_at timestamptz not null default now()
);

create table projects (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  name text not null,
  domain_schema_version text not null,
  created_at timestamptz not null default now()
);

create table documents (
  id uuid primary key,
  project_id uuid not null references projects(id),
  source_type text not null,
  canonical_uri text,
  title text,
  authors jsonb not null default '[]',
  published_at timestamptz,
  current_version_id uuid,
  status text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table document_versions (
  id uuid primary key,
  document_id uuid not null references documents(id),
  content_hash char(64) not null,
  object_key text not null,
  parser_version text not null,
  extracted_metadata jsonb not null default '{}',
  page_count int,
  created_at timestamptz not null default now(),
  unique(document_id, content_hash)
);

create table chunks (
  id uuid primary key,
  document_version_id uuid not null references document_versions(id),
  ordinal int not null,
  text_hash char(64) not null,
  text_object_key text not null,
  page_start int,
  page_end int,
  section_path text,
  token_count int,
  metadata jsonb not null default '{}',
  unique(document_version_id, ordinal)
);

create table ingestion_jobs (
  job_id uuid primary key,
  tenant_id text not null,
  project_id text not null,
  user_id text not null,
  filename text not null,
  media_type text,
  byte_size bigint not null check (byte_size > 0),
  content_hash char(64) not null,
  staging_key text not null,
  status text not null check (status in (
    'queued', 'running', 'retry_scheduled', 'succeeded', 'failed', 'cancelled'
  )),
  attempt int not null default 0,
  max_attempts int not null,
  available_at timestamptz not null,
  lease_owner text,
  lease_expires_at timestamptz,
  document_id uuid,
  deduplicated boolean,
  can_retry boolean not null default false,
  error_code text,
  error_message text,
  created_at timestamptz not null,
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz not null
);

create unique index ingestion_jobs_active_content_idx
on ingestion_jobs (tenant_id, project_id, content_hash)
where status in ('queued', 'running', 'retry_scheduled');

create table agent_runs (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  project_id uuid not null references projects(id),
  user_id uuid,
  session_id text not null,
  task_type text not null,
  input_text text not null,
  input_hash char(64) not null,
  config_snapshot jsonb not null,
  trace_id text,
  status text not null,
  final_output jsonb,
  error_code text,
  started_at timestamptz not null default now(),
  finished_at timestamptz
);

create table run_events (
  id bigserial primary key,
  run_id uuid not null references agent_runs(id),
  sequence_no int not null,
  event_type text not null,
  payload jsonb not null,
  created_at timestamptz not null default now(),
  unique(run_id, sequence_no)
);

create table evidence_items (
  id uuid primary key,
  run_id uuid not null references agent_runs(id),
  chunk_id uuid references chunks(id),
  source_type text not null,
  quote text not null,
  locator jsonb not null,
  retrieval_path jsonb not null default '{}',
  relevance_score double precision,
  support_score double precision,
  created_at timestamptz not null default now()
);

create table claims (
  id uuid primary key,
  run_id uuid not null references agent_runs(id),
  claim_text text not null,
  claim_type text not null,
  strength text not null,
  status text not null,
  evidence_ids uuid[] not null default '{}',
  created_at timestamptz not null default now()
);

create table memory_items (
  id uuid primary key,
  project_id uuid not null references projects(id),
  memory_type text not null,
  content jsonb not null,
  source_run_id uuid references agent_runs(id),
  source_refs jsonb not null default '[]',
  trust_score double precision not null default 0,
  status text not null default 'candidate',
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table skills (
  id uuid primary key,
  project_id uuid not null references projects(id),
  name text not null,
  description text not null,
  current_version_id uuid,
  status text not null,
  created_at timestamptz not null default now()
);

create table skill_versions (
  id uuid primary key,
  skill_id uuid not null references skills(id),
  version text not null,
  definition jsonb not null,
  source_run_ids uuid[] not null default '{}',
  eval_summary jsonb not null default '{}',
  status text not null,
  parent_version_id uuid references skill_versions(id),
  created_at timestamptz not null default now(),
  unique(skill_id, version)
);

create table eval_cases (
  id uuid primary key,
  project_id uuid not null references projects(id),
  case_type text not null,
  input jsonb not null,
  expected jsonb not null,
  tags text[] not null default '{}',
  source text not null,
  created_at timestamptz not null default now()
);

create table eval_runs (
  id uuid primary key,
  asset_type text not null,
  asset_version_id uuid,
  baseline_version_id uuid,
  metrics jsonb not null,
  regressions jsonb not null default '[]',
  status text not null,
  created_at timestamptz not null default now()
);

create table audit_logs (
  id bigserial primary key,
  tenant_id uuid not null references tenants(id),
  actor_type text not null,
  actor_id text,
  action text not null,
  resource_type text not null,
  resource_id text,
  payload jsonb not null default '{}',
  created_at timestamptz not null default now()
);
```

### 4.3 Neo4j 图谱模型

节点标签：

```text
Project, Document, DocumentVersion, Chunk, Author, Institution,
Method, Task, Dataset, Metric, Experiment, Claim, Concept,
Skill, Memory, User
```

核心关系：

```text
(Document)-[:HAS_VERSION]->(DocumentVersion)
(DocumentVersion)-[:HAS_CHUNK]->(Chunk)
(Document)-[:AUTHORED_BY]->(Author)
(Author)-[:AFFILIATED_WITH]->(Institution)
(Document)-[:PROPOSES]->(Method)
(Document)-[:EVALUATES]->(Method)
(Method)-[:TARGETS]->(Task)
(Method)-[:EVALUATED_ON]->(Dataset)
(Method)-[:REPORTS]->(Metric)
(Method)-[:IMPROVES_OVER]->(Method)
(Document)-[:CITES]->(Document)
(Concept)-[:RELATED_TO]->(Concept)
(Claim)-[:SUPPORTED_BY]->(Chunk)
(Claim)-[:CONTRADICTS]->(Claim)
(Skill)-[:DERIVED_FROM]->(AgentRun)
```

每条关系必须至少保存：`project_id`、`confidence`、`source_chunk_ids`、`extractor_version`、`valid_from`、`valid_to`、`created_at`。没有来源片段的实体关系只能进入 `candidate` 子图，不能直接参与高置信答案。

当前 P1 实现采用两层图模型。结构层保留 `Document -> HAS_CHUNK -> Chunk`；语义候选层将抽取实体写为文档作用域的 `Entity`，将关系统一写为 `SEMANTIC_RELATION` 并以 `relation_type` 属性表达具体谓词。候选 ID 由 scope、document、规范名称和关系三元组通过 UUID5 生成，可安全重放。跨文档同一实体不会被破坏性合并，而是先生成 `EntityResolutionCandidate`，批准后投影为 `ENTITY_RESOLUTION`/`same_as` 证据边。`pending`/`rejected`/`archived` 关系和归并均不能通过 allowlisted traversal，只有 `approved` 映射为 `status=active`。关系或归并批准会同时批准两端实体，已拒绝或已归档实体会阻止晋级；拒绝实体会级联拒绝所有尚未归档的关联关系和归并，保证审核控制面与查询投影一致。

本地 `graph_candidates.json` 是当前审核控制面的原子审计仓，v2 保存实体候选、关系候选、归并候选和 append-only review event；读取路径兼容 v1，并在下一次写入时原子迁移。所有 JSON read-modify-write 事务同时持有进程内 `asyncio.Lock` 和同目录 `flock`，防止多个 Outbox worker 各自读取旧快照后互相覆盖；锁文件不删除，数据文件仍以临时文件原子替换。resolver v2 每个新实体只连接一个确定性最佳代表；pending resolution 维护为高置信边优先的最小森林，保留全部已审核记录，并通过 scope 参数化维护查询删除 Neo4j 冗余 pending 投影。候选列表默认每类返回 500 条并在响应头报告总数，禁止再次返回数十 MB 无界控制面。Neo4j 是可重建的查询投影。两者都强制 tenant/project/document scope，候选关系和归并只保存 `source_chunk_ids`，查询时回连同作用域 active Chunk 组装 `EvidenceRef`。生产阶段仍应将控制面迁入 Postgres/outbox，Neo4j 保持投影职责，避免把文件锁扩展成分布式数据库。

约束和索引示例：

```cypher
create constraint document_id_unique if not exists
for (n:Document) require n.id is unique;

create constraint chunk_id_unique if not exists
for (n:Chunk) require n.id is unique;

create constraint method_key_unique if not exists
for (n:Method) require (n.project_id, n.canonical_name) is node key;

create index chunk_project_idx if not exists
for (n:Chunk) on (n.project_id);

create index entity_alias_idx if not exists
for (n:Method) on (n.aliases);
```

如果使用 Neo4j 向量索引作为图内补充检索，向量维度必须与 embedding 模型一致；主向量检索仍放在 Qdrant，避免把 Qdrant 和 Neo4j 同时当成两个不一致的主索引。

### 4.4 Qdrant collection 设计

每个 project 默认一个 collection：`{prefix}{project_id}_chunks`。每个 point 的 payload：

```json
{
  "chunk_id": "uuid",
  "document_id": "uuid",
  "document_version_id": "uuid",
  "project_id": "uuid",
  "title": "GraphRAG ...",
  "source_type": "pdf",
  "published_at": "2025-01-01",
  "section_path": ["Methods", "Retrieval"],
  "page_start": 4,
  "page_end": 5,
  "entity_ids": ["method:...", "dataset:..."],
  "text_hash": "sha256",
  "parser_version": "parser-1",
  "embedding_revision": "embed-1"
}
```

推荐三阶段检索：

1. dense semantic vector：召回语义相近片段。
2. sparse vector：保留专有名词、缩写、论文标题、版本号和公式关键词。
3. late interaction 或 cross-encoder reranker：只对候选集重排，不对全库运行。

Qdrant 官方 Query API 支持多向量、prefetch、RRF/DBSF 融合和多阶段查询。第一版用 dense+sparse+RRF，第二版再加入 late interaction。所有过滤都必须包含 `project_id`，并对常用 payload 建索引。

当前活动实现使用 `hermesgraph_chunks_v3_idf` collection 和两个 named vectors：`dense`、`sparse`。dense 可由 OpenAI embedding 或 deterministic test encoder 产生；sparse 为稳定 hashed lexical encoder，并在 collection schema 启用 `Modifier.IDF`，避免全量语料下原始词频放大高频项。tenant、project、document、status 建 keyword payload index。相同 scope filter 同时传入两路 `Prefetch`、主 `query_points`，并在结果返回后由应用再做一次 fail-closed payload 校验。Qdrant 服务端执行 RRF；外层 LangChain Integration Runtime 再对 `qdrant_hybrid` 与内置项目资料做带 branch weight 的融合。schema 或 embedding revision 变化时通过 shadow collection 重建和评测后切换，不原地混写。

deterministic dense encoder 不是生产语义模型。它的候选在应用层还必须通过非停用词词项交集门槛，防止 server RRF 将唯一但无关的 dense 候选变成有效证据；OpenAI 语义 embedding 不启用该离线限制。生产切换模型/维度时要使用新的 collection 或执行显式迁移，并把 embedding revision 写入 trace；不能静默复用旧 collection。

## 5. 文档入库流水线

### 5.0 当前 durable 异步纵向链路（已实现）

P1 已升级为 Docker Postgres job control plane + Qdrant + Neo4j 纵向闭环。同步 endpoint 仍保留，但 Compose 和工作台默认走异步 job：

```text
multipart upload
  -> bounded read
  -> suffix / UTF-8 / JSON / PDF validation
  -> atomic scope-hashed staging
  -> Postgres enqueue / scoped content coalescing
  -> worker claim (FOR UPDATE SKIP LOCKED)
  -> owner lease + heartbeat
  -> SHA-256 content hash and document dedup
  -> Document IR parser / loader fallback
  -> hierarchical token-aware chunker / LangChain text fallback
  -> deterministic document/chunk UUID5
  -> atomic raw object + Postgres metadata/chunks transaction
  -> transaction outbox event
  -> coordinated Qdrant dense/sparse upsert
  -> Neo4j Document-HAS_CHUNK-Chunk MERGE
  -> rule/structured entity-relation candidate extraction
  -> deterministic cross-document resolution proposals
  -> atomic candidate audit store + Neo4j candidate/resolution projection
  -> human/eval review gate (approved only becomes active)
  -> LangChain weighted RRF with builtin_lexical branch
  -> EvidenceRef(user_asserted, content_hash, page/chunk locator)
  -> owner-checked complete / retry_scheduled / failed
```

- 支持 `.pdf`、`.md`、`.markdown`、`.txt`、`.json`、`.csv`、`.html`、`.htm`；HTML 忽略 script/style/noscript/svg，PDF 保留页码。
- PDF/arXiv 路径使用 `document-ir-pdf-v1` 和
  `hierarchical-token-chunker-v2:o200k_base:min80`：按标题 section 切分，保留 heading path、页区间、
  source block IDs、OCR 方法与 warning；相邻短 section 在同一文档内有界打包，chunk 连同上下文不超过
  400 tokens。普通文本仍可走 LangChain `RecursiveCharacterTextSplitter` fallback。
- 同一 tenant/project 下以原始字节 SHA-256 去重。文档和 chunk ID 基于 scope、hash 和序号确定性生成，重试不会制造新身份。
- 原文保存在受控 `knowledge/uploads/<scope_hash>/...` object key，存储路径执行 traversal guard。Compose 使用 Postgres 保存 metadata/chunk；旧 JSON index 只作为一次性迁移源和离线降级 backend。
- 检索时再次强制 tenant/project filter。用户上传证据固定为 `TrustLevel.USER_ASSERTED`，不能覆盖系统指令，也不能自动进入 verified 层级。
- `DELETE` 是逻辑归档。归档同时设置本地 document、Qdrant payload、Neo4j Document/Chunk/relationship 状态；原文和审计记录仍保留，再次上传可重新激活确定性身份。
- Qdrant 与 Neo4j 并行写入；任一后端失败会对所有后端执行补偿归档，本地 document 标记 `failed`，API 返回 503，避免半发布索引。
- Neo4j 已实现结构化 `Document -> Chunk` 证据子图、规则/OpenAI/混合实体关系候选投影、确定性跨文档 resolver、人工批准/拒绝、审核事件、来源回连和归档。当前 resolver 只读取 pending/approved endpoint，并只接受稳定标识符、规范化名称和显式别名重合；OpenAI structured-output extractor v6 已通过 5-case 合同集和 18-case/14-source 自然 arXiv live gate。完整 PDF 全库 backfill、隐式别名/歧义消解、开放领域本体和 eval 自动晋级仍未完成。
- job submission、lease 恢复、自动重试、取消和人工重试已完成。共享 Postgres migration、knowledge metadata/chunk repository 与 transaction outbox 已实现并通过真实 adapter contract；旧数据导入、dispatcher 和 Docker 重启验收均已完成。S3 对象存储、版本化增量索引、staging 垃圾回收和更大规模多 worker 压测仍是生产目标。

### 5.0.1 存量 Document IR 与 Chunk 迁移（已实现）

`KnowledgeRechunkService` 是不调用模型、不启动图谱抽取的存量迁移器。每篇文档按以下顺序执行：

```text
checkpoint/hash/parser revision validation
  -> load document-ir-pdf-v1
  -> hierarchical token-aware chunking
  -> upsert new Qdrant points
  -> delete stale points for the same document
  -> atomic Postgres replace_chunks
  -> knowledge.document.rechunked audit event
  -> fsync + atomic checkpoint update
```

向量先写、Postgres 后替换，使数据库永远不会引用尚未建索引的新 chunk；Qdrant replacement 会先
scroll 旧 point IDs，再 upsert 新集合并删除差集。失败文档保留旧 Postgres chunks，并在 manifest 中
记录错误以供续跑。进程级 lock 防止两个迁移器同时写同一 checkpoint；文档 source hash、IR revision、
chunker revision 和结果计数均进入 checkpoint。全量 v2 迁移处理 528/528 篇、0 error，将 64,240 个
中间 v1 chunks 收敛为 43,850 个 active chunks，其中 8,126 个由相邻短 section 有界打包；最大 chunk
为 400 tokens。25 个低文本页完成 Vision 后，只强制重切受影响的 7 篇，旧 642 chunks 替换为 664，
当前项目总数为 43,872。

稀疏配置变化使用 shadow collection，不原地修改 schema。`hermesgraph-reindex-knowledge` 从 Postgres
读取 active chunks，将 `default` 与 `computer-science` 重建到启用 Qdrant `Modifier.IDF` 的
`hermesgraph_chunks_v3_idf`，门禁通过后再成对切换 `QDRANT_COLLECTION` 和
`QDRANT_SPARSE_IDF`。当前 collection 共 43,903 points（`computer-science` 43,872 + `default` 31）；
旧 collection 保留用于快速回滚。

### 5.0.2 结构图谱重投影与候选证据对账（已实现）

chunk ID 包含 parser/chunker revision，因此重新切分后不能只更新 Postgres 和 Qdrant。结构层使用
`GraphStructureReindexService` 执行有界、可续传的 Postgres-to-Neo4j 投影：

```text
list scoped active documents
  -> checkpoint content hash/parser version/chunk count validation
  -> bounded load of retained chunks
  -> MERGE active Document/Chunk/HAS_CHUNK in batches of 200
  -> archive same-document Chunk/HAS_CHUNK not in active_chunk_ids
  -> fsync + atomic per-document checkpoint
```

`hermesgraph-reindex-graph-structure` 支持 `--dry-run`、`--limit`、重复 `--document-id`、`--force`、
`--concurrency`、`--fail-fast` 和独占进程锁，不构造或调用模型 client。2026-07-28 实跑 528/528 成功、
43,872 chunks、0 error；Neo4j 中 528/43,872/43,872 个 Document/Chunk/HAS_CHUNK active，旧
32,129 Chunk/关系 archived，active chunk 只有一个 parser/chunker revision。

语义候选采用 replacement revision 语义。`save_batch` 先确认新批次存在，再归档同 scope/document 下
未出现在新批次中的旧 pending；approved/rejected 不被新抽取覆盖，review event 保留。Neo4j 在
semantic MERGE 前执行同样的 stale-pending 归档。跨文档 resolver 只读取 pending/approved entity，
防止 archived/rejected endpoint 继续产生 `same_as`。

对于历史迁移，`hermesgraph-reconcile-graph-candidates` 从 Postgres 收集当前 active chunk IDs，一次事务
扫描 JSON 审核仓，并以参数化 Cypher 同步扫描 Neo4j。任一 evidence chunk 不再 active 的 pending
entity/relation/resolution 会归档；reviewed 状态不变。命令提供 `--dry-run`，可重复运行且结果幂等。
由于候选 JSON 仓位于 Docker `app_data:/data`，生产维护和全量 graph backfill 必须在 app 容器内
执行，不能让主机 `.data` 形成第二个审核仓。当前已归档 12,920/925/4,989 个旧候选，二次 dry-run
六项计数均为 0。

### 5.1 当前 job 状态机与生产目标

```text
queued -> running -> succeeded
queued/retry_scheduled -> cancelled
running -> retry_scheduled -> running
running -> failed
failed/cancelled --manual retry when can_retry--> queued
expired running lease -> running on next claim, or failed at max attempts
```

claim 使用 `FOR UPDATE SKIP LOCKED`，完成/失败只接受同一 `lease_owner`。active-content partial unique index 与事务 advisory lock 保证同 scope/content hash 的并发提交只创建一个活跃 job。永久解析/staging 错误不自动重试；索引和未知基础设施错误按上限 1 小时指数退避。

更细的 document-version 生产状态仍按以下模型演进：

```text
DISCOVERED
  -> DOWNLOADING
  -> STORED
  -> PARSING
  -> CHUNKING
  -> EXTRACTING
  -> EMBEDDING
  -> INDEXING
  -> PUBLISHED

任意阶段 -> RETRYABLE_FAILED -> RETRYING
任意阶段 -> PERMANENT_FAILED
PUBLISHED -> STALE -> PARSING
```

### 5.2 处理步骤

1. 规范化 URL、计算幂等键、检查已有 content hash。
2. 将原始内容写入对象存储，保存 MIME、大小、来源、抓取时间和校验和。
3. 解析 PDF/HTML/Markdown，保留页码、章节、表格、代码块和原始 offset。
4. 按语义边界切分；默认目标 500-900 tokens，overlap 80-120 tokens，章节和表格不跨边界拼接。
5. 生成 chunk metadata，并写入 Postgres。
6. LLM/规则混合抽取实体、关系、断言和引用；所有抽取结果都带 source chunk。
7. 执行实体规范化、别名合并和候选关系去重。
8. 生成 dense/sparse embedding，批量写入 Qdrant。
9. 使用 Neo4j MERGE 写入图谱，候选关系保留 `status=candidate`。
10. 运行质量检查，全部通过后将 document version 标记为 `published`。
11. 通过 outbox 事件通知缓存失效、统计更新和学习服务。

### 5.3 抽取 schema

```python
class ExtractedEntity(BaseModel):
    name: str
    canonical_name: str
    entity_type: Literal[
        "method", "task", "dataset", "metric", "author",
        "institution", "concept", "experiment"
    ]
    aliases: list[str] = []
    confidence: float = Field(ge=0, le=1)
    evidence_chunk_ids: list[str]

class ExtractedRelation(BaseModel):
    source_entity: str
    relation: str
    target_entity: str
    confidence: float = Field(ge=0, le=1)
    evidence_chunk_ids: list[str]
    qualifier: dict[str, str | float | int | bool] = {}

class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    relations: list[ExtractedRelation]
    claims: list[str]
```

抽取结果必须先通过 Pydantic schema 验证，再进入 entity resolver；不能直接把模型输出当作 Cypher 参数。

当前代码中的对应合同为 `GraphEntityCandidate`、`GraphRelationCandidate`、`GraphExtractionBatch` 和 `EntityResolutionCandidate`。抽取项包含 `tenant_id`、`project_id`、`document_id`、`source_chunk_ids`、`confidence`、`extractor_revision` 与候选状态；归并项额外固定两端 entity/document ID、匹配策略和 `resolver_revision`。所有 extractor 实现相同 `EntityRelationExtractorPort`，resolver 实现 `EntityResolverPort`，两者都不能绕过 `GraphCandidateService` 直接写 active Neo4j 事实。

#### 5.3.1 OpenAI 严格结构化抽取（已实现，待 live eval）

运行模式由 `GRAPH_EXTRACTOR_MODE` 控制：

| 模式 | 执行路径 | API key | 适用场景 |
| --- | --- | --- | --- |
| `rule` | `RuleBasedEntityRelationExtractor` | 不需要 | 默认离线演示、稳定回放、显式关系基线 |
| `openai` | `OpenAIStructuredEntityRelationExtractor` | 必须 | 生产语义候选抽取 |
| `hybrid` | 两者并行，按稳定 ID 融合 | 必须 | 兼顾规则 precision 与模型 recall |

模型调用使用 OpenAI Responses API 的原生 Pydantic 解析：

```python
response = await client.responses.parse(
    model=graph_extraction_model,
    input=[
        {"role": "system", "content": UNTRUSTED_EXTRACTION_POLICY},
        {"role": "user", "content": json.dumps(document_and_chunks)},
    ],
    text_format=StructuredGraphDraft,
    max_output_tokens=max_output_tokens,
    store=False,
)
```

`StructuredGraphDraft` 及其 entity/relation 子模型均 `extra=forbid` 且字段必填。实体类型使用有限 Literal；关系只接受 2-64 字符的 lower_snake_case；名称、aliases、候选数、证据数、rationale 和 confidence 都有上限。输入按字符预算分批，输出再受 token、实体总数和关系总数限制，防止单个文档无界放大。

安全与正确性门禁按以下顺序执行：

1. system instruction 明确 chunk 是 untrusted data，禁止服从正文指令、跨文档消歧、补充世界知识或伪造事实。
2. 文档正文只出现在独立 JSON user payload；每个 chunk 带当前请求分配的 UUID。
3. Responses structured output 先通过 Pydantic schema；`refusal`、`incomplete`、缺少 parsed output 都失败关闭。
4. 应用再次验证每个候选的 `source_chunk_ids` 是当前 document batch 的非空子集。含未知 ID 的候选不会进入候选仓。
5. 合法草稿被转换成 scope 完整、UUID5 稳定、`status=pending` 的 domain candidate；schema 正确不代表事实正确，仍必须经过人工/eval review。
6. ingestion 中任一抽取异常触发既有多索引补偿：归档已写 Qdrant/Neo4j 投影，本地文档标记 `failed`，不返回伪成功。

`HybridEntityRelationExtractor` 使用 `asyncio.gather` 并行执行规则与模型抽取。实体 ID 由 scope、document、entity type 和规范名称生成；关系 ID 由两端实体 ID 与谓词生成，因此两路结果可以确定性合并 aliases、Chunk evidence、最高 confidence 和 rationale。融合层拒绝非 pending 候选、scope 不一致批次和 identity collision。它属于 ingestion 数据流，不是 Agent，不使用 LangChain `create_agent` 或 LangGraph。

bootstrap 只在 `openai/hybrid` 模式创建独立 `AsyncOpenAI` 客户端，并配置有界 timeout、两次 SDK retry 和显式 close。`OPENAI_API_KEY` 缺失时 Settings 直接拒绝启动。工作台 overview 暴露当前模式，便于运维确认。默认 Compose 保持 `rule`，所以没有 key 仍可运行完整离线成品。

当前测试覆盖稳定 ID、pending-only、证据子集、提示注入的数据/指令隔离、拒答、未完成、严格 JSON schema、rule/model 融合、配置门禁和 client close。机器判定的中英文 golden gate 已实现并跑通 rule 基线；真实模型的 entity/relation precision、拒答率、schema failure、延迟与 token 成本仍无报告，必须完成 live eval 后才能把该项标为生产质量通过。

### 5.4 跨文档实体归并控制面（已实现）

归并的目标不是把两个 `Entity` 节点立即压成一个节点，而是建立可撤销、可解释的 identity link。文档作用域实体保留各自名称、类型、来源和抽取版本；这使单个来源归档、抽取器升级、审核反转和历史回放都不需要恢复被覆盖的数据。

当前 resolver 流程：

```text
stored extraction batch
  -> list same-scope active/pending/approved entities
  -> require different document and identical entity_type
  -> compare canonical and alias forms
  -> stable UUID5 proposal with evidence from both documents
  -> graph_candidates.json v2 (status=pending)
  -> Neo4j ENTITY_RESOLUTION projection (status=candidate)
  -> GraphCandidateService review
  -> approved => status=active, relation_type=same_as
  -> rejected/archive => isolated but audit retained
```

匹配策略按可信度从高到低为：

| 策略 | 条件 | 默认 confidence | 自动发布 |
| --- | --- | ---: | --- |
| `exact_identifier` | `Identifier`/`SoftwareSymbol` 的 NFKC + casefold 名称完全一致 | 0.99 | 否 |
| `exact_name` | 同类型规范名称完全一致 | 0.98 | 否 |
| `normalized_name` | 去分隔符后的名称一致，且长度不少于 3 | 0.95 | 否 |
| `alias_overlap` | 规范名称/显式别名集合存在可靠交集 | 0.92 | 否 |

当前实现刻意不使用编辑距离、向量近邻或 LLM 自评直接生成 `same_as`。这些方法适合扩大召回，但必须先作为新的 proposal strategy 进入固定评测集，测量 pair precision、cluster purity、false merge rate 和人工驳回率，再决定是否允许进入候选仓。

`EntityResolutionCandidate` 的稳定身份由以下元组生成：

```text
(tenant_id, project_id, min(entity_id_a, entity_id_b),
 max(entity_id_a, entity_id_b), resolver_revision)
```

两端排序使摄取顺序不影响 candidate ID。候选至少包含两个不同 Chunk ID；repository 再验证两端实体真实存在、scope 与 document ID 一致、且来自不同文档。重复 proposal 合并证据和最高 confidence，同时保留 approved/rejected 审核字段；archived proposal 在来源重传后只恢复为 pending，不自动恢复过去的 active 状态。

状态机与级联规则：

```text
pending -> approved | rejected
approved -> rejected
rejected -> pending
archived -> terminal until deterministic re-ingestion
```

- 批准归并时，两端 pending 实体由同一 reviewer 联动批准，各自写独立 review event。
- 任一端点为 rejected/archived 时，归并批准 fail closed。
- 拒绝实体会级联拒绝所有尚未归档的关联 `SEMANTIC_RELATION` 和 `ENTITY_RESOLUTION`。
- 归档任一来源文档会归档相关实体、关系和归并；另一文档自己的独立事实仍可查询。
- Neo4j 写入失败时执行状态补偿；JSON 审核仓是当前控制面事实源，Neo4j 可以从它重建。

批准的 `same_as` 边携带两端 Chunk ID。allowlisted `paths` 查询可以在 1-3 hop 内跨越该边，并要求路径中每个节点、关系及证据 Chunk 都属于相同 tenant/project 且为 active。这样跨文档扩展不会绕过原有 evidence-first 发布门禁。

## 6. 检索系统设计

### 6.1 查询理解

输入先转换为严格 `QueryPlanDraft`。OpenAI planner 通过 Responses API
`responses.parse(..., text_format=QueryPlanDraft, store=False)` 产生结构化结果；user query 以独立
JSON 载荷发送并被声明为不可信数据，planner 不接收检索内容，也不能回答问题或请求外部动作。
模型结果仍要经过应用层去空、去重、长度和上限校验。拒答、超时、429、`incomplete`、空 parsed
output 或 schema 错误会记录异常类型，并降级到可回放的确定性 planner：

```python
class QueryPlanDraft(StrictModel):
    intent: Literal[
        "lookup", "compare", "synthesis", "personal_recall", "visual_lookup"
    ]
    subqueries: list[str] = Field(min_length=1, max_length=4)
    fallback_queries: list[str] = Field(max_length=4)
    required_terms: list[str] = Field(max_length=12)
    minimum_evidence: int = Field(default=1, ge=1, le=20)
    minimum_distinct_sources: int = Field(default=1, ge=1, le=4)
    requires_visual_evidence: bool = False
    recommends_graph_search: bool = False

class RetrievalGap(StrictModel):
    sufficient: bool
    evidence_count: int = Field(ge=0)
    distinct_source_count: int = Field(ge=0)
    visual_evidence_count: int = Field(ge=0)
    covered_terms: list[str] = []
    missing_terms: list[str] = []
    reasons: list[str] = []
```

控制器硬上限是每轮最多 4 个子查询、最多 2 轮；在线 Agent 对同一 run 最多调用该能力 3 次。
上限由配置 schema、controller constructor 和 Agent tool wrapper 三层执行，不能由模型计划扩大。
`recommends_graph_search` 只是可观测建议，v1 不允许 planner 生成 Cypher 或直接调 Neo4j。

### 6.2 检索流水线

```mermaid
sequenceDiagram
    participant A as Hermes Agent
    participant C as Agentic Retrieval Controller
    participant P as Structured Planner
    participant L as LangChain Runnable dataflow
    participant Q as Scoped Hybrid Retrieval
    A->>C: one complete information goal + RunContext
    C->>C: enforce tenant/project filters
    C->>P: untrusted query only
    P-->>C: strict plan or controlled failure
    C->>C: sanitize or deterministic fallback
    C->>L: up to 4 subqueries
    par LCEL abatch
        L->>Q: subquery 1 + enforced scope
    and
        L->>Q: subquery 2..4 + enforced scope
    end
    Q-->>L: partial results/errors
    L-->>C: bounded bundles
    C->>C: cross-query RRF + evidence gap check
    alt coverage insufficient and new fallback exists
        C->>L: second-round fallback queries
        L-->>C: bounded bundles
        C->>C: RRF + final gap check
    end
    C-->>A: RetrievalBundle + decision trace
```

底层 `RetrievalPipeline` 继续执行 Qdrant dense/sparse server-side RRF、相关性门槛和 scope
复核。控制器通过 LangChain `RunnableLambda.abatch` 并行调用稳定的 RetrievalPort，并把单分支错误
收敛为 trace，不让某个子查询异常取消其他分支。来自不同子查询的证据再按稳定 evidence identity
执行 cross-query RRF；重复证据只保留一份，并在 metadata 中记录支持它的 round/query/rank。

每轮融合后执行确定性缺口检查：证据数量、按 `document_id` 或去掉 `#chunk` 的 source root
计算的独立来源数、视觉证据数和 required term 覆盖。required term 当前只提供诊断和 fallback
构造，不单独让已有证据判失败；强制门槛是数量、来源多样性和显式视觉要求。停止原因固定为
`coverage_satisfied`、`no_new_queries` 或 `round_limit`，避免隐式无限反思。

### 6.3 结果合同

```python
class EvidenceRef(BaseModel):
    evidence_id: UUID
    text: str
    title: str | None
    score: float
    provenance: Provenance
    metadata: dict[str, Any]

class GraphPath(BaseModel):
    nodes: list[dict]
    relationships: list[dict]
    evidence: list[EvidenceRef]

class RetrievalBundle(BaseModel):
    query: str
    evidence: list[EvidenceRef]
    graph_paths: list[GraphPath]
    applied_filters: dict
    trace: dict

class GraphEntityMatch(BaseModel):
    node: GraphNode
    matched_text: str
    matched_field: Literal["canonical_name", "alias"]
    score: float
    evidence: list[EvidenceRef]

class GraphRAGResult(BaseModel):
    query: str
    resolved_entities: list[GraphEntityMatch]
    graph_paths: list[GraphPath]
    evidence: list[EvidenceRef]
    trace: dict[str, Any]

class WebSearchRequest(BaseModel):
    query: str
    max_results: int

class WebSearchResult(BaseModel):
    query: str
    summary: str
    evidence: list[EvidenceRef]
    sources: list[WebSearchSource]
    trace: dict
```

工具只返回 `RetrievalBundle`，不返回未经裁剪的全文。`trace` 记录 controller/planner revision、
planner fallback error 类型、严格计划、每轮 queries/result counts/errors、新增/累计证据数、每轮 gap、
停止原因、已执行 query 数和图搜索建议。`ToolEvent.detail` 只保留这些决策数据，不复制 evidence
正文；完整证据仍走本轮 publisher allowlist。工作台 Run Inspector 展示 intent、轮次、停止原因、
planner、queries 与缺口，历史运行可以据此回放和比较。

### 6.4 融合与重排

单查询底层 pipeline 和跨查询 controller 都使用 Reciprocal Rank Fusion：

```text
rrf(d) = sum_i weight_i / (k + rank_i(d))
```

底层分支权重由 retrieval pipeline 配置；跨查询 controller v1 对每个子查询等权，并固定
`rrf_k=60`。权重和 k 都不是常量真理，必须在真实 eval 集上优化。重排器只能重新排序已召回
候选，不能从候选外引入内容。

### 6.5 图谱查询模板

禁止让模型自由生成任意 Cypher。通过有限模板实现：

- `find_entity`: canonical name + alias + type。
- `neighbors`: 指定实体、关系白名单、最大深度。
- `method_dataset_metric`: 方法到数据集到指标的两跳路径。
- `method_lineage`: 方法改进、继承、基线关系。
- `citation_subgraph`: 文献引用和共同引用。
- `conflicting_claims`: 同一主题下相互冲突的断言。

所有模板执行前检查：tenant/project、label 白名单、relationship 白名单、max hops、limit、timeout；执行后再做 evidence join。

当前 Neo4j adapter 已实现的固定模板是：`neighbors`、`paths(max_hops=1..3)`、`conflicts`。路径深度只从预编译的 1/2/3 模板选择，实体、scope 和 limit 全部作为参数，不接受模型生成的 Cypher。每条返回关系必须通过同 scope 的 `Chunk` 节点和 `source_chunk_ids` 组装出至少一个 `EvidenceRef`；缺证据、跨 scope、端点不在路径中的记录都会在应用层被拒绝。上面的领域模板属于后续 DomainPack 扩展目标。

#### 6.5.1 GraphRAG Tool Suite（已实现）

图谱读取分为两层。低层 `GraphSearchPort` 只执行预编译遍历模板；高层
`GraphRetrievalToolkit` 把多个受控读取组合成 Agent 可理解的语义操作。工具层不接收
tenant/project，也不接收 Cypher、label expression 或 relationship expression。

| 工具 | 输入 | 内部步骤 | 输出 |
| --- | --- | --- | --- |
| `resolve_graph_entities` | mentions、可选 entity types、min score、limit | canonical/alias 双向包含匹配、固定分数、Chunk evidence join、scope 复核 | 排序后的 `GraphEntityMatch` |
| `retrieve_evidence_subgraph` | query、可选 seed entities、1-3 hops、text/path limits | 实体解析、文本检索、allowlisted paths、无证据路径剔除、来源身份去重 | `GraphRAGResult` |
| `compare_graph_entities` | left/right entity、1-3 hops、limit | 两端解析、两次邻居查询、连接路径查询、集合运算 | 连接路径、共享/独有邻居和证据 |
| `search_graph` | entities、template、hops、limit | 单次固定模板查询 | 原始 evidence-backed paths |

实体解析分数是确定性的，不由 LLM 自报：canonical exact `1.00`、alias exact `0.96`、输入包含
canonical `0.90`、输入包含 alias `0.88`、canonical 包含输入 `0.78`、alias 包含输入 `0.74`。
默认阈值为 `0.65`。Neo4j 只读取 `Entity(status=active)`，并要求实体的
`source_chunk_ids` 能 join 到同 scope、active 且正文非空的 `Chunk`；本地合同实现从相邻关系聚合
证据。两种 backend 都拒绝没有证据的解析结果。

联合检索流程：

```mermaid
sequenceDiagram
    participant H as Hermes Agent
    participant B as Run-scoped Bridge
    participant C as Capability Registry
    participant T as GraphRetrievalToolkit
    participant R as Scoped Retrieval
    participant G as Neo4j Evidence Graph
    H->>B: retrieve_evidence_subgraph(query, seeds, bounds)
    B->>B: global + graph budget, duplicate fingerprint
    B->>C: typed capability + RunContext
    C->>C: graph:read + knowledge:read, schema, timeout, bytes
    C->>T: GraphRAGRequest
    par bounded retrieval
        T->>R: query + enforced scope + top_k
    and
        T->>G: resolve canonical/alias entities
    end
    T->>G: paths(resolved seed names, max_hops, path_limit)
    T->>T: scope/evidence recheck + provenance identity dedupe
    T-->>B: GraphRAGResult
    B->>B: add returned IDs to run-local evidence allowlist
    B-->>H: resolved entities + paths + evidence + trace
```

当前 toolkit 的读取在同一 capability handler 内有界执行；图与文本 backend 的错误不会被模型
重试逻辑无限放大。`MAX_GRAPH_TOOL_CALLS=6` 是 bridge 侧每 run 上限，仍同时受
`MAX_TOOL_CALLS`、Capability timeout、`MAX_TOOL_OUTPUT_BYTES` 和相同输入 fingerprint 限制。
`retrieve_evidence_subgraph` 额外要求 `knowledge:read`，只有 `graph:read` 会在执行 backend 前失败。

应用层的最后一道路径门禁要求：路径至少一条关系；所有 node/relationship 与 RunContext 的
tenant/project 完全一致；每条 relationship 的 evidence 非空。融合去重优先使用
`chunk_id + content_hash + text`；没有 Chunk 身份时回退到
`source_type + source_id + content_hash + text`，避免 Qdrant/Neo4j 的 source ID 格式差异或 Neo4j
projection 临时 evidence UUID 让同一来源重复扩张。对比工具只把同时包含左右实体 node ID 的路径标为连接路径，
联合子图的每条路径也必须包含至少一个已解析 seed node ID，避免同名 Chunk 产生未锚定结构路径；
共享/独有邻居通过 node ID 集合运算产生，不让 LLM 猜测拓扑。

### 6.6 受控公共 Web Search

联网检索不是把 provider hosted tool 直接挂到 Hermes 根 Agent。直接挂载会让 provider 生成的 citation
绕过现有 `allowed_evidence -> AnswerPublisher` 白名单。当前实现把 Responses API
`{"type": "web_search"}` 封装在 `OpenAIHostedWebSearch` 中，再由
`IntegrationRuntime` 注册为 `search_web@1.0.0`：

```text
Hermes plugin tool search_web
  -> WebSearchRequest(query, max_results)
  -> CapabilityRegistry(web:read, timeout, output bytes)
  -> OpenAI Responses hosted web_search (tool_choice=required, store=false)
  -> URL citation annotations
  -> public URL + domain policy validation
  -> run-scoped EvidenceRef(untrusted)
  -> allowed_evidence
  -> AnswerPublisher
```

安全和发布合同：

1. 查询最多 2,000 字符；疑似 private key、API key、access token 或常见 provider token 在发网前拒绝。
2. prompt 与网页都视为 untrusted data；adapter 不执行网页动作、不抓取 citation URL，也不把网页指令
   交给系统层。
3. 仅接受 `http/https` 公网 URL；拒绝 userinfo、localhost、`.local` 和非 global IP。
4. deployment domain allowlist 同时在 provider 请求和返回端执行；清理 fragment、`utm_*`、
   `fbclid/gclid` 后再去重。
5. `action.sources` 只作补充统计，因为兼容 provider 可能返回空列表；可发布证据以
   `message.output_text.annotations[type=url_citation]` 为主。
6. 每个 URL 聚合其 citation context，生成稳定 UUID5、content hash、run ID、provider/model/
   response revision 和 citation spans。原始未引用摘要不返回给 Agent。
7. `search_web` 有独立 `MAX_WEB_SEARCH_TOOL_CALLS`，同时受全局工具预算、Capability timeout 和
   `MAX_TOOL_OUTPUT_BYTES` 约束。失败只记录错误类型，不记录查询原文。
8. Web evidence 固定 `TrustLevel.UNTRUSTED`。模型若输出 `verified` claim/confidence，
   publisher 会确定性降级为 `supported`；直接绕过 publisher 构造 verified Web answer 会失败。

历史上当前兼容端点曾通过 provider-level 单次 live citation gate 与完整 Agent API 纵向验收。
版本化 Web golden set 已在 11.0.2 节实现；当前重新验收时最小 live case 经两次尝试均返回 HTTP
503，因此 adapter/contract 已交付，当前 provider 的 live 生产晋级仍未获批。

### 6.7 Agentic RAG 冻结基线

截至 2026-07-29，当前检索系统被定义为“有界、证据优先的 Agentic RAG v1”。成立依据是它已经
实现 typed query plan、意图锚定、多子查询并行混合检索、跨查询 RRF、证据缺口判断、有界补检、
GraphRAG 工具选择和 run-scoped evidence publish gate，而不是因为使用了 Hermes 或 LangChain。

当前不能升级为“生产级 Agentic GraphRAG”：生产 embedding 尚未通过 live gate；全库语义 KG
仍在 pending backfill/review；`required_terms` 缺失尚未成为硬 gap；Publisher 尚未执行
claim-evidence entailment；学习型 reranker 和真实 Hermes 检索发布纵向门禁仍缺失。RAG 策略与
代码开发暂时冻结，KG backfill 只作为不改变策略的数据维护任务继续。恢复开发必须从
[`AGENTIC_RAG_LOCK.md`](./AGENTIC_RAG_LOCK.md) 的 `RAG-001` 开始并按 phase gate 推进。

## 7. Hermes Agent 在线运行时

### 7.1 Sidecar 与运行相关性

`HermesAgentRuntime` 是现有 `AgentRuntime` 的实现，Hermes 作为独立 sidecar 运行。一次请求的
确定性路径如下：

1. `RunService` 创建带 tenant/project/user/session 的 `RunContext`。
2. `HermesCapabilityBridge.open_run` 生成不可预测的 `hg_<run_id>_<nonce>` bridge ID，并只在应用
   内存保存真实作用域、工具预算和 evidence allowlist。
3. adapter 向 Hermes `POST /v1/runs`，把 bridge ID 作为 Hermes `session_id`，并显式传入同
   tenant/project/user/session 的 bounded `conversation_history`。稳定的
   `X-Hermes-Session-Key` 由 tenant/project/user/session 与 bridge secret 做 HMAC 得到，用于保持
   Hermes 长期会话记忆，但不暴露原始个人标识。
4. adapter 优先消费 `GET /v1/runs/{id}/events` SSE；传输失败时回退到状态轮询。取消会调用
   `/stop`，任意 approval request 默认调用 `/approval` 拒绝。
5. Hermes 前台 bridge tool 通常使用 bridge `task_id`；后台 `bg-review` fork 会生成临时
   `task_id`，所以 native audit 必须使用父 `session_id` 关联 bridge。插件与 sidecar 分别使用
   `HERMES_BRIDGE_TOKEN` 和 `HERMES_API_KEY`，两个 secret 不复用。
6. 首次 `hermesgraph_publish_answer` 成功后，adapter 立即向用户返回冻结的
   `AnswerResponse`，但不调用 `/stop`。重复 publish 幂等返回且不能覆盖首个 artifact；其他业务工具
   仍被拒绝。
7. Hermes 主 loop 正常完成后触发原生 Memory/Skill review。`bg-review` 的 `on_session_end` 向
   bridge 发送 completion event；应用在该事件或 bounded timeout 前保留 state，然后释放。
8. Hermes 没有调用严格发布工具即失败，不能使用其自由文本终态兜底。

当前不启用 delegation、nested specialist 或 session search。复杂检索由 Hermes 单根 Agent 调用
HermesGraph 的有界 Agentic Retrieval Controller；长时摄取和学习由 Postgres worker 承担。

### 7.2 严格回答发布

模型输出与最终公开响应必须分层。Hermes 最终必须调用
`hermesgraph_publish_answer` 并提交严格 draft：

```python
class AgentAnswerDraft(BaseModel):
    answer_markdown: str
    claims: list[dict]
    citation_ids: list[UUID]
    confidence: Literal[
        "verified", "supported", "inferred", "insufficient", "conflicting"
    ]
    limitations: list[str]
    followup_queries: list[str]
```

Capability Bridge 使用本轮 `allowed_evidence` 白名单把 ID hydrate 为完整
`AnswerResponse.citations`。模型不能
生成或覆盖 source ID、canonical URI、page、trust、privacy 或视觉坐标。未知 ID、claim 引用了
未公开 citation、或高置信回答没有证据时，publisher 必须失败关闭。`verified` 还是额外的
provenance trust 断言：claim 的 supporting evidence 与最终 citations 都必须是
`TrustLevel.VERIFIED`；模型把 user-asserted/observed/untrusted 证据标成 verified 时，正常
publish 路径会降级为 supported，直接调用 validate 的非法对象则被拒绝。

### 7.3 受信任插件工具

```python
class SearchKnowledgeInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)

class SearchGraphInput(BaseModel):
    entities: list[str] = Field(min_length=1, max_length=10)
    template: Literal["neighbors", "paths", "conflicts"] = "neighbors"
    max_hops: int = Field(default=2, ge=1, le=3)
    limit: int = Field(default=20, ge=1, le=100)

class ResolveGraphEntitiesInput(BaseModel):
    mentions: list[str] = Field(min_length=1, max_length=10)
    entity_types: list[str] = Field(default_factory=list, max_length=10)
    min_score: float = Field(default=0.65, ge=0.0, le=1.0)
    limit: int = Field(default=10, ge=1, le=50)

class RetrieveEvidenceSubgraphInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    seed_entities: list[str] = Field(default_factory=list, max_length=10)
    entity_types: list[str] = Field(default_factory=list, max_length=10)
    max_hops: int = Field(default=2, ge=1, le=3)
    top_k: int = Field(default=10, ge=1, le=50)
    path_limit: int = Field(default=30, ge=1, le=100)

class CompareGraphEntitiesInput(BaseModel):
    left_entity: str = Field(min_length=1, max_length=500)
    right_entity: str = Field(min_length=1, max_length=500)
    max_hops: int = Field(default=3, ge=1, le=3)
    limit: int = Field(default=30, ge=1, le=100)

class WebSearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=8, ge=1, le=20)

class RecallProjectMemoryInput(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=20)

class ActivateGovernedSkillInput(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")

class WorkspaceFileReadInput(BaseModel):
    root: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    path: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=400)
```

工具实现要求：

1. 使用 Pydantic 进行输入和输出校验。
2. tenant/project/user 只从 bridge 内保存的 `RunContext` 读取；工具 schema 不接受模型指定 scope。
3. 记录 tool name、参数 hash、耗时、结果大小、错误类型，不默认记录敏感原文。
4. 设置总工具预算、分工具预算、timeout、最大返回字节数和重复输入检测。
5. 只返回稳定的领域合同，不暴露底层库异常和密钥。
6. 外部公开网络查询不能携带 tenant/project 私有内容；查询和 URL 还要经过 provider adapter 的
   敏感信息与公网策略校验。
7. 调用 `hermesgraph_publish_answer` 后禁止任何继续检索；重复发布失败关闭。
8. Computer Workspace 只暴露显式配置 root。实现拒绝绝对路径、`..`、隐藏/凭据路径、私钥后缀和
   symlink，并使用 `O_NOFOLLOW`、文件/页数/ZIP 解压/扫描/输出预算；PDF/DOCX/XLSX 读取不调用模型。
9. workspace read/search 结果生成 `workspace_file` provenance 和本轮 EvidenceRef，未经 publisher
   allowlist hydration 不能支撑 supported claim。

内部 callback 端点仅在 Hermes runtime 启用时注册：

```text
GET  /internal/hermes/health
POST /internal/hermes/runs/{bridge_id}/tools/{tool_name}
POST /internal/hermes/runs/{bridge_id}/events
```

所有端点要求 bridge bearer token，并只接受仍在保留期内的 bridge ID。

### 7.4 停止与降级

停止条件按以下顺序判断：

1. 已有足够证据覆盖要求的 claim。
2. 证据审计通过，或明确返回不足/冲突。
3. 达到 `max_turns`、`max_tool_calls`、总预算或总超时。
4. 检测到重复 query、重复 tool input、发布后工具调用或无效循环。

达到限制时，Hermes 应通过严格发布工具输出“已完成部分 + 缺口 + 是否建议异步继续”。若模型网关、
sidecar、SSE、bridge、schema 或发布门禁失败，run 必须进入失败态，不能发布未经验证的自由文本。

## 8. LangChain Integration Runtime

LangChain 负责整个项目中间层的组件标准化和数据流衔接：

- `Document` 作为内部文档对象。
- loaders 负责把外部来源转换为统一文档。
- `langchain-text-splitters` 负责切分策略。
- retriever 或 Runnable 包装 Qdrant 查询。
- `RunnableParallel` 并行 dense、sparse、metadata 分支。
- `RunnableLambda` 负责结果标准化和去重。
- `RunnableBranch` 根据已验证的 `QueryPlan` 选择检索分支。
- `ChatPromptTemplate` 管理可版本化 Prompt 片段和 Prompt Capsule 渲染。
- provider model adapter 可用于 ingestion、reflection、entity extraction 等后台结构化任务。
- callbacks/config metadata 贯穿 Runnable，关联 `run_id`、租户、技能版本与成本。
- LangChain tool/retriever 统一转换为内部 `Capability`，再由 HermesGraph bridge 插件暴露给 Hermes。
- retry、fallback、并行和分支只作用于有界中间流水线，不接管最终对话循环。

明确禁止在在线链路中使用 LangChain `create_agent`、LangGraph `ToolNode` 或循环型 StateGraph 包裹 Hermes run。LangChain agent middleware 依赖 LangChain 自身 Agent Loop，因此不直接用于在线主链路；需要的日志、重试、脱敏和预算能力通过 Runnable callbacks、Hermes plugin hook、Capability Bridge 及项目中间件实现。若未来需要可恢复的长事务工作流，单独引入 Temporal、Dapr 或 DBOS 作为 durable orchestration 层。

## 9. 证据与回答系统

### 9.1 Claim 生命周期

```text
EXTRACTED -> NORMALIZED -> EVIDENCE_LINKED -> VERIFIED
                                      ├── SUPPORTED
                                      ├── INFERRED
                                      ├── CONFLICTING
                                      └── INSUFFICIENT
```

### 9.2 证据验证规则

- `SUPPORTED`：来源直接表达或可进行非常短的逻辑归纳，且来源可靠。
- `INFERRED`：需要 Agent 跨来源或跨图路径推断，必须显式标记。
- `CONFLICTING`：不同来源对同一限定条件下的结论不一致。
- `INSUFFICIENT`：存在相关片段，但不能支持该 claim。
- `VERIFIED` 不是“绝对正确”，只代表通过当前证据规则。

### 9.3 引用覆盖率

将最终回答拆成 claim 集合 `C`，每个 claim 关联证据集合 `E(c)`：

```text
citation_coverage = count(c where E(c) is non-empty and support(c) >= threshold) / count(C)
```

默认只把 coverage 达到 0.9 且没有高风险 unsupported claim 的答案标记为 `supported`。不满足时输出 `insufficient` 或要求 Agent 再检索。

## 10. Hermes 原生学习与 HermesGraph 治理学习

### 10.1 四类记忆

| 类型 | 内容 | 存储 | 默认 TTL |
| --- | --- | --- | --- |
| Episodic | 一次任务的输入、计划、工具、证据、反馈 | Postgres + 对象存储 | 180 天，可配置 |
| Semantic | 可验证事实、实体别名、用户偏好 | Postgres + Qdrant/Neo4j | 来源或项目生命周期 |
| Procedural | 技能定义、触发条件、步骤、限制 | Git-like 文件 + Postgres | 版本化，无自动删除 |
| Policy | 工具选择、检索权重、停止规则的候选策略 | Postgres | 版本化，需门禁 |

### 10.2 任务后学习流水线

```mermaid
flowchart LR
    RUN[Completed Run] --> CAP[Capture Trace]
    CAP --> EVAL[Evaluate]
    EVAL --> REF[Reflect on Failure/Success]
    REF --> MEM[Candidate Memory]
    REF --> SK[Skill Candidate]
    REF --> CASE[Regression Case]
    MEM --> GATE[Write Gate]
    SK --> REPLAY[System Offline Replay]
    CASE --> REPLAY
    REPLAY --> SHADOW[Shadow Observation]
    SHADOW --> APPROVE1[Human Approval]
    APPROVE1 --> CANARY[Canary Traffic]
    CANARY --> HEALTH[Activated-run Health Gate]
    HEALTH --> APPROVE2[Human Approval]
    APPROVE2 --> ACTIVE[Active]
    HEALTH -->|Regression| ROLLBACK[Automatic Rollback]
    GATE --> STORE[Memory Store]
    ACTIVE --> RUNTIME[Next Agent Run]
```

当前实现由 `LearningEngine` 与 `SkillEvolutionService` 分工：前者保存轨迹、执行反思、写入受门禁 Memory、挖掘稳定 Skill Draft 和 ChangeSet；后者生成不可由 API 调用方伪造的 `SkillEvaluation`，执行顺序晋级、记录 `SkillObservation`、聚合 `SkillHealthReport` 并触发自动回滚。`observe` 模式停在 Draft；`shadow/canary/active` 学习模式会在新 Draft 产生后自动完成安全评测和来源轨迹回放，最多推进到 Shadow。

OpenAI Structured Reflection 使用 Responses `parse` 和 Pydantic 严格 schema。模型输入是有界且显式标记为 untrusted 的轨迹摘要；模型不能设置 tenant/project/user、run ID、provenance、memory key、评估结果或 Skill 状态。普通成功运行只执行 deterministic reflection；显式反馈、失败状态、纠错/审计/安全标签才触发模型反思。拒答、incomplete、timeout 或 provider 错误返回 deterministic 结果，并在 ChangeSet 中记录不含错误正文的类型链。

### 10.3 何时生成技能

单次成功不能直接生成可发布技能。默认条件：

- 相似任务至少出现 3 次。
- 至少 2 次获得用户正反馈或评估器通过。
- 任务中存在可抽象的稳定步骤，而非只依赖某一篇文档。
- 工具调用序列的编辑距离低于阈值，且结果质量稳定。
- 技能不含秘密、个人敏感数据、任意 Shell、任意 SQL/Cypher 或未经批准的外部写入。

### 10.4 技能格式

```yaml
name: compare_research_methods
version: 0.1.0
description: Compare two research methods with evidence-backed dimensions.
trigger:
  intents: [compare]
  entities: [method]
  phrases: ["A 和 B 怎么选", "方法对比", "优缺点"]
preconditions:
  min_evidence_chunks: 4
  require_primary_sources: false
steps:
  - action: search_hybrid
    input: "retrieve each method and its canonical definition"
  - action: search_graph
    template: method_dataset_metric
  - action: fetch_evidence
    input: "retrieve definition, mechanism, result, limitation evidence"
  - action: verify_evidence
    input: "verify every comparison claim"
  - action: synthesize
    output_schema: compare_response
constraints:
  max_tool_calls: 12
  require_citation_coverage: 0.9
  no_write_tools: true
evaluation:
  success_metrics: [citation_coverage, claim_support, task_completion]
  regression_cases: [compare-001, compare-002]
promotion:
  required_status: shadow
  human_approval: true
```

技能是声明式策略，不是可执行代码。只有内置白名单 action 能被解释器执行。

### 10.5 技能晋级

```text
DRAFT
  -> SECURITY_REVIEW
  -> OFFLINE_PASS
  -> SHADOW
  -> CANARY_10_PERCENT
  -> ACTIVE
  -> DEPRECATED / ROLLBACK
```

晋级门禁至少检查：

- 核心质量指标不得低于 baseline 超过阈值。
- unsupported claim rate 不上升。
- tool error、latency、cost 不超预算。
- 不能访问新权限范围。
- 不能绕过证据校验和人工审批。
- 通过 prompt injection、memory poisoning、越权和数据外泄测试。

当前 `DeterministicSkillEvaluator` 保留兼容类名，实际 revision 为 `counterfactual-skill-replay-v2`。它先对 Skill 声明做安全扫描，再由 `FrozenCapabilitySkillSandbox` 把来源 run 的非控制 `ToolEvent` 冻结为只读 fixture，并通过真实 `SkillActivationRegistry -> SkillExecutionRegistry` 逐步执行候选。沙箱限制总超时、最大步骤、工具输出字节数和声明能力；动作错序、fixture 耗尽/剩余、历史工具失败、执行失败或预算越界全部失败关闭。报告只保存 input/output SHA-256、fixture index、错误码、耗时、序列相似度和工具成功率，不持久化原始工具输出。

该回放验证“候选声明式步骤在相同冻结能力结果下是否可完成”，没有调用真实网络、数据库写工具或模型 provider，因此无外部副作用且可重复。它不等同于开放世界模型重跑；后续 provider/tool 仿真必须使用新 sandbox/evaluator revision，不能覆盖 v2 报告。

Shadow 观测对匹配查询执行无副作用的 frozen-fixture sandbox evaluation，不能调用在线工具。
Canary/Active 健康度只统计运行快照中钉住该版本且实际产生 `activate_governed_skill`（兼容历史
`activate_skill`）事件的样本；仅曝光未激活的运行不计入质量分母。默认 Shadow 至少 3 个有效样本、
Canary 至少 5 个实际激活样本，阈值全部可配置。

每次健康计算生成不可变 `SkillPromotionEvidence`，冻结 scope、版本、观测窗口、全部原始观测、
按 run 去重后的有效观测、run ID、evaluator revision、baseline/candidate、unsupported claim、
失败率、反馈率、全部阈值和建议动作。重复的 run outcome 不扩大样本量；同一 run 后续显式反馈生成
新观测并取代该 run 的旧观测进入有效窗口，但旧记录仍保留审计。晋级、拒绝、健康阻断和回滚事件都
保存对应 evidence，tenant/project/version 不一致直接校验失败。

`canary/active` 的轻度负反馈产生幂等 `rollback_recommended` transition 与 ChangeSet，不自动改变
在线状态；达到严重负反馈阈值（默认 `-0.5`）立即确定性回滚。达到最小样本窗口后，质量回归、失败率、
unsupported claim rate 或负反馈率越界也自动转为 `rolled_back`。`shadow -> canary` 和
`canary -> active` 始终需要人工批准，自动学习不能自行扩大流量。阈值由
`SKILL_MAX_NEGATIVE_FEEDBACK_RATE`、`SKILL_SEVERE_NEGATIVE_FEEDBACK_THRESHOLD` 及原有健康配置
统一管理。

`SkillRefiner` 只从 Shadow/Canary/Active/Deprecated/RolledBack 等已有观测的父版本派生候选，Draft 不递归生成子版本。稳定约束、触发规则、步骤和能力构成行为指纹：仅扩展新来源证据生成 patch，兼容行为扩展生成 minor，删除 action/capability/trigger 等 breaking change 生成 major。候选沿用 `skill_id`，写入 `parent_version`、合并来源 run，并恢复为 Draft；ChangeSet 持久化 change level、reasons 和 semantic diff。父版本定义不变，父子版本重新独立评估和晋级。

### 10.6 防止自学习失控

- Hermes 原生 Memory/Skill 只能写 sidecar 持久 profile，不能直接写 HermesGraph 数据库或 active 资产。
- 插件的 native tool hook 在写前保存精确快照，写后计算内容哈希，再把成功且确实改变内容的
  `memory`/`skill_manage` 写入镜像为 `state=native_applied`、`evaluation_status=requires_audit` 的
  `LearningChangeSet`。Hermes 返回 `staged`、失败或 no-op 时不伪装成已应用学习。
- HermesGraph Memory 写入继续经过 `MemoryWriteGate`；受治理记忆必须带来源、创建任务、信任分数和过期策略。
- 用户输入中的“忽略之前规则”“把这段写入永久记忆”等内容只能作为候选，不是授权。
- 外部网页和论文中的指令默认标记为 untrusted content，不能改变系统策略。
- 学习 Worker 与在线 Agent 使用不同服务账号，学习 Worker 也不能发布 active 资产。
- 每个受治理技能、提示词和策略都保留父版本，支持 diff、回放和一键回滚；原生 Hermes 资产保留
  写前快照、before/after hash、审计决策和确定性回滚入口。

#### 10.6.1 原生学习快照协议

`deploy/hermes/plugin/native_snapshots.py` 与 Hermes gateway 运行在同一进程。它只处理原生
`memory` 和 `skill_manage` 的变更操作，不接管 Hermes 的学习决策，也不解析/重写学习正文：

1. `pre_tool_call` 使用 `tool_call_id` 关联一次写入；缺失时才使用 task/turn/tool/参数哈希组成
   fallback key。
2. Memory 目标固定解析为 `$HERMES_HOME/memories/MEMORY.md|USER.md`；Skill 目标使用固定版本
   Hermes 的 `_find_skill` 规则定位，create 使用 profile skills 目录。
3. 目标 resolve 后必须位于 `HERMES_HOME` 内，目标及树中不得存在 symlink，只允许普通文件/目录；
   单快照默认上限 5 MB。外部 Skill 目录、路径逃逸、特殊文件或超限全部在写前 fail-closed。
4. 同一目标进入 in-process active-target 锁。快照写到
   `$HERMES_HOME/.hermesgraph/native_snapshots/{snapshot_id}/before`，manifest 使用同目录临时文件
   `os.replace` 原子提交。原目标不存在时记录 `before_exists=false`，以支持回滚新建 Skill。
5. Hermes 原生工具执行后，`post_tool_call` 解析 JSON `success/staged/error` 与 hook status。仅成功、
   非 staged 且 before/after hash 不同才标记 `applied=true`；随后 manifest 进入 `ready`。
6. 插件发往应用的事件只保留 action/name/target/file_path 等控制字段。`content`、`file_content`、
   `old_text`、`old_string`、`new_string` 和嵌套 batch 内容统一替换为 `{redacted,length,sha256}`；
   tool result 只保留 success/staged/done/error 摘要。
7. 应用把成功事件追加为原始 `LearningChangeSet`，不修改 Hermes 文件，也不把 Hermes 原生资产误报
   为通过 HermesGraph Draft/Shadow/Canary/Active 门禁。

快照状态机：

```text
pending
  -> ready              write succeeded and after_hash verified
  -> no_change          success result but content hash did not change
  -> after_hash_failed  write may have happened, rollback disabled, requires audit
  -> deleted            tool failed or write was only staged

ready -> rolled_back    only when current_hash == recorded after_hash
```

`after_hash_failed` 是保守状态：审计仍可看到原生写入，但系统不会承诺可自动回滚。快照保存在
`hermes_data` volume，容器重建不丢失。自动 GC 只接受下文 10.6.4 的终态与期限；`pending`、
`after_hash_failed` 和未审阅的 `ready` 永不自动清理。

#### 10.6.2 确定性回滚协议

Hermes 插件在 gateway 进程内启动标准库 `ThreadingHTTPServer`，监听容器内 `8643`。该端口不发布到
宿主机，只允许应用使用第三份独立 bearer token 调用：

```text
GET  /health
POST /v1/native-snapshots/{snapshot_id}/accepted
POST /v1/native-snapshots/{snapshot_id}/rollback
POST /v1/native-snapshots/gc
Body: {"expected_after_hash": "<64-char sha256>"}
```

`accepted` 额外接收 `retention_days`；`gc` 只接收 `dry_run`。三个写端点都只存在于内网 admin，
公开 FastAPI 不暴露删除操作。

回滚必须同时满足：snapshot manifest 为 `ready`、请求哈希等于 manifest `after_hash`、目标当前哈希
仍等于 `after_hash`、目标没有原生写入或另一回滚正在进行。任一条件不满足返回 409，不覆盖后续合法
学习。恢复时，文件通过同目录临时文件替换；目录先构建完整 restore tree，再在同一文件系统内交换，
恢复后重新计算 `before_hash`。新建资产的 before 状态不存在，回滚会删除该资产。Skill 恢复后清空
Hermes skill prompt cache；Memory 继续遵守 Hermes 的 frozen-session 语义，下一 session 使用恢复内容。

回滚成功把 manifest 标记为 `rolled_back`。相同请求在目标仍等于 before 状态时幂等返回成功；回滚后
又发生写入时再次请求返回 409。应用无权发送任意路径或任意内容，只能发送不可猜测 snapshot ID 与
expected hash，实际路径和 before bytes 始终留在 sidecar volume。

#### 10.6.3 审核账本与失败语义

`HermesNativeLearningService` 从 append-only ChangeSet 派生状态，不新增可被原地覆盖的状态表：

```text
native base ChangeSet                  status=pending
  + review(outcome=accepted)           status=accepted
  + review(outcome=rolled_back)        status=rolled_back
  + review(outcome=rollback_failed)    status=rollback_failed
```

接受和已经完成的回滚都是幂等操作。每次首次接受、成功回滚或失败回滚都会追加
`target_type=hermes_native_review` 的新 ChangeSet，原生 base ChangeSet 保持不可变。允许先接受后回滚；
已回滚资产不能再接受。失败尝试保留 reviewer/reason/detail，但不保存 token、绝对路径或学习正文。

| 场景 | 处理 | 对外状态 |
| --- | --- | --- |
| snapshot 前路径逃逸、symlink、超限 | 阻止原生写工具 | tool blocked，无 applied ChangeSet |
| Hermes 返回 staged/failed | 删除未完成快照 | 不生成 native applied ChangeSet |
| 写成功但 after hash 失败 | 保留 manifest，禁止自动回滚 | pending audit，rollback_supported=false |
| 审核时目标已发生后续写入 | 不恢复 before bytes | HTTP 409 + rollback_failed review |
| 8643 不可达或鉴权失败 | 不在应用侧模拟文件操作 | HTTP 503 + rollback_failed review |
| 回滚成功后重复请求 | 校验当前 before hash | 幂等 200，不追加重复成功 review |

#### 10.6.4 快照容量、保留与备份

快照生命周期由 Hermes 进程内的 `NativeSnapshotManager` 持有，审阅语义由应用的 append-only
ChangeSet 持有。接受审阅时应用使用 snapshot ID、`after_hash` 和配置的保留天数调用内部
`accepted`；返回的 `retention_until` 随 review ChangeSet 一起保存。登记失败时审阅仍可接受，但
manifest 不会获得 `review_state=accepted`，因此 GC fail-safe 为不删除，并在 review detail 中记录
生命周期登记延期。

| 快照状态 | 默认保留 | 自动 GC 条件 |
| --- | ---: | --- |
| `pending` / `after_hash_failed` | 无期限 | 永不自动删除，必须人工处置 |
| `ready` 且未审阅 | 无期限 | 永不自动删除 |
| `ready + accepted` | 30 天 | `purge_after` 到期 |
| `rolled_back` | 7 天 | 回滚完成且 `purge_after` 到期 |
| `no_change` | 24 小时 | `created_at` 到期 |

单个 before snapshot 默认最多 5 MB，整个目录默认最多 1 GB。每次原生写入前先执行安全 GC，再计算
目录真实字节数；利用率达到 80% 时 `/health.storage.capacity_status=warning`，达到 100% 为
`critical` 并 fail-closed 阻止新的原生 Memory/Skill 写入。容量失败会删除本次未完成目录并释放目标
锁，不留下半快照。`GET /v1/hermes/native-learning/health` 只转发计数、字节数、状态分布、GC 候选数
和备份元数据；它不返回绝对路径、正文、哈希或 token。

Hermes 0.19.0 的 full backup 遍历整个 `HERMES_HOME`，其排除目录不包含 `.hermesgraph`，所以
`.hermesgraph/native_snapshots` 随 `hermes_data` 的完整备份进入归档。生产备份仍必须加密，并在独立
临时 volume 执行恢复演练；“被归档包含”不等于“恢复已验证”。

### 10.7 Hermes 形态的运行细节

项目现在直接运行 Hermes，而不是只模仿其形态。学习有两个所有权边界：

1. Hermes 原生通道拥有个人 profile Memory、原生 Skill、Todo 和后台回顾。`hermes_data` volume
   保持跨容器重启；稳定 HMAC session key 保持同一用户会话连续性。
2. HermesGraph 治理通道拥有项目 Memory、检索策略、证据规则、Prompt、图谱事实、版本化 Skill、
   Evaluation/Observation/Transition 和外部副作用权限。
3. 原生写入是先应用后审计；治理写入是先生成候选、再评测晋级。二者都必须能关联来源 run 和 scope，
   但状态语义不能混用。
4. 每个完成、失败、取消或收到反馈的 run 仍提交 Postgres durable learning job。昂贵 OpenAI
   reflection 仅在显式反馈、非成功终态或纠错/审计/安全标签触发；普通成功 run 可使用 deterministic
   reflection。重复模式由 Skill miner 处理。
5. 工具大结果写入 artifact store，Hermes 只接收有界预览、hash 和 evidence ID；最终 citation 由
   HermesGraph publisher 从 allowlist 补全。
6. Hermes native hook 会在用户可见 artifact 返回后由后台回顾触发。应用不重新打开已完成 run 的
   SSE 事件流，但用父 `session_id` 关联审计，并等待明确 completion event 后再释放 bridge。
7. `RuntimeCapsuleProvider` 在 run start 使用已经钉住的 Skill version 构建相关 discovery index；
   Hermes 通过 `activate_governed_skill` 取回精确 Canary/Active 定义。服务端拒绝未钉住、版本漂移、
   scope 不符或非运行态 Skill。返回只包含声明式步骤/能力/约束，不执行脚本，也不建立第二循环。
8. 激活工具受独立 run 预算，事件写入 trajectory；health gate 只把真实激活计入 Canary/Active
   分母。这样治理 Skill 的评测与在线效果形成闭环，同时不混淆 Hermes 原生 Skill 存储。

### 10.8 学习触发器

```text
即时触发：用户纠错、显式正/负反馈、证据审计失败、安全事件
聚类触发：相似任务 >= 3，成功样本 >= 2，工具序列稳定
周期触发：累计 10 个已完成 run 或 24 小时维护窗口
禁止触发：单次偶然成功、只有模型自评、缺少来源、含敏感信息
```

触发只代表可以生成候选，不代表候选可以发布。评测器与生成器不得共享唯一判断来源，必须同时使用确定性指标、固定回归集和必要的人工审批。

### 10.9 自进化控制 API

```text
POST /v1/projects/{project_id}/skills/{skill_id}/evaluate?skill_version={semver}
GET  /v1/projects/{project_id}/skill-evolution
GET  /v1/projects/{project_id}/skills/{skill_id}/evaluations?skill_version={semver}
GET  /v1/projects/{project_id}/skills/{skill_id}/transitions?skill_version={semver}
POST /v1/projects/{project_id}/skills/{skill_id}/transition?skill_version={semver}
GET  /v1/projects/{project_id}/hermes/native-learning
POST /v1/projects/{project_id}/hermes/native-learning/{change_set_id}/review
GET  /v1/hermes/native-learning/health
```

`skill_version` 是可选 query 参数；省略时为向后兼容解析同 ID 最新版本，跨版本 UI、评估、晋级和审计必须显式传入。`evaluate` 生成并持久化系统报告；`transition` 不接受调用方提供的 evaluation，未知字段由 Pydantic `extra=forbid` 拒绝。Shadow -> Canary 和 Canary -> Active 同时要求健康门禁与 `human_approved=true`，不能跳级。工作台只展示并调用这些 scoped API，不读取 JSON/Markdown repository。

原生学习 review body 为 `{decision: "accept"|"rollback", reviewer_id, reason}`，同样使用
`extra=forbid`。列表按 tenant/project 过滤，跨 project 的 change_set_id 返回 404；快照不可回滚或发生
hash drift 返回 409；sidecar admin 不可用返回 503。当前后端 API 已实现，专用操作 UI 留作后续，
不影响 API 和审计账本使用。

### 10.10 Durable Learning Job

异步部署中，`RunService` 的终态与反馈路径只保存轨迹并提交任务，不直接执行模型反思或 Skill 演化：

```text
Run completed / failed / cancelled / feedback
  -> immutable RunTrajectory snapshot
  -> SHA-256(trigger + canonical snapshot)
  -> Postgres learning_jobs v4/v5
  -> SKIP LOCKED claim
  -> owner + UUID fencing token lease
  -> Reflection -> checkpoint(reflection_completed, v7)
  -> transaction[Postgres artifacts(v6) + versioned links(v9/v10) + checkpoint(artifacts_committed)]
  -> transaction[Observation + links(v9) + checkpoint(observations_committed)]
  -> transaction[Evaluation + Skill state + transition(v8) + versioned links(v9/v10)
                 + checkpoint(evolution_committed)]
  -> result refs / retry / failed
```

同一 trigger 与 snapshot 只产生一个逻辑任务；反馈改变 snapshot 后产生新任务。领取按 `available_at, created_at` 排序；v5 partial unique index 强制同 scope/run 同时最多一个 `running` job，即使两个 worker 在 `SKIP LOCKED` 下竞争也不能并发执行 completion 与 feedback。每次 claim 都生成新的 fencing token，续租、完成和失败必须同时匹配 owner 与 token，旧执行不能确认新一代任务。

Compose 默认 `LEARNING_ARTIFACT_BACKEND=postgres`。v6 保存 Memory、Skill、Evaluation、Observation、ChangeSet 和旧文件 import ledger；旧 JSON/Markdown 只按内容哈希导入一次，切换后不双写。所有 worker artifact transaction 先锁定并验证 `job_id + worker_id + lease_token + 未过期 lease`。Memory 的逻辑身份为 scope/user/type/key，撤销后重试不能复活；Skill 同 name/version 只允许状态迁移，定义 hash 不可变化；Evaluation、Observation 和 ChangeSet 以稳定 UUID 与语义 payload hash 构成 immutable ledger，同 ID 不同内容直接失败。

v7 checkpoint 是单调聚合状态，只允许 `reflection_completed -> artifacts_committed -> observations_committed -> evolution_committed` 逐级推进。Reflection artifact 保存 trajectory hash、deterministic evaluation、模型/降级 revision、候选 Memory 和触发原因；重试从该 artifact 重建 `ExperienceReflection`，不再次调用模型。`LearningEngine.learn()` 保持 inline 兼容，durable worker 使用拆分后的 `reflect/apply_reflection`。Evaluation fingerprint 排除 Skill status/创建时间，ChangeSet identity 包含语义 mutation hash，避免 promotion 或 feedback 重放制造错误冲突。

v8 新增 `learning_skill_transitions` append-only ledger。每条记录保存 Skill/version、promotion/rollback/health_gate、from/to、allowed、applied、reasons、evaluation ID、human approval、learning job ID 和决定时间；worker 事件 ID 由 job+skill+状态迁移稳定生成，手工 API 尝试保留独立事件。允许、拒绝和自动 rollback 都记录，状态表不再承担历史职责；`GET /v1/projects/{project_id}/skills/{skill_id}/transitions` 只读返回同作用域历史。

v9 新增 task-local `PostgresLearningTransaction` 和 `learning_job_artifact_links`。durable worker
在每个确定性 stage 开启一个 asyncpg transaction，并通过 context-local connection 让 Memory、
ChangeSet、Skill、Observation、Evaluation、Transition 的全部 repository 读写复用该事务；最后才
锁定并验证 job owner、fencing token 与 lease，再写累计 checkpoint 和 artifact links。最终 fence
失败、stage 抛错或进程在 commit 前终止时，资产、Skill 当前状态、transition ledger、links 与
checkpoint 一起回滚。读路径也复用同一 connection，因此一次 evolution stage 内的连续状态迁移
能看到自己的未提交写入。

`learning_jobs.reconciliation_status` 使用
`not_required/pending/verified/required`。`hermesgraph-reconcile-learning` 从 checkpoint 重建
派生 links，检查 Memory、ChangeSet、Skill、Observation、Evaluation、Transition 的作用域和存在性，
并验证 succeeded result 与最终 checkpoint 完全一致。缺 link 可安全补建；artifact 缺失、冲突 link
或 result 不一致只标记 `required`，绝不猜测补写业务资产。

v10 为 `LearningJobCheckpoint` 与 `LearningJobResult` 增加 `skill_candidate_version`，并为
`learning_job_artifact_links` 增加 `artifact_version`。新任务的 Skill link 必须携带精确 SemVer；
reconciliation 以 `(skill_id, version, tenant, project)` 验证候选，不能因同一 ID 的其他版本存在而
误判。旧 v9 checkpoint 仍可读取；当 checkpoint 已有版本而旧派生 link 为空时，对账器只补齐 link
版本，不猜测或重建 Skill 本体。

任务仍采用 at-least-once delivery。Reflection 的外部模型调用放在数据库事务外；模型响应返回但
`reflection_completed` checkpoint 尚未提交的极小窗口仍可能重复调用 provider。后续确定性
Postgres effects 已按 stage 原子提交，但系统仍不能宣称跨外部模型与 Postgres exactly-once。

控制 API：

```text
GET    /v1/projects/{project_id}/learning-jobs
GET    /v1/projects/{project_id}/learning-jobs/{job_id}
DELETE /v1/projects/{project_id}/learning-jobs/{job_id}
POST   /v1/projects/{project_id}/learning-jobs/{job_id}/retry
```

公开响应不包含 trajectory、lease owner 或 fencing token。取消仅作用于 queued/retry_scheduled；running 任务由 lease 和后续协作取消扩展处理。

### 10.11 MemoHarness 风格经验固定化（Phase 1-4、MH-014 已实现）

项目采用 MemoHarness 的双层经验银行和六维 Harness 诊断思想，但不引入第二 Agent runtime：

```text
terminal RunTrajectory
  -> immutable per-case Experience E
  -> deterministic D1-D6 diagnosis
  -> repeated-pattern Draft G
  -> offline replay -> shadow -> canary -> active
  -> next run start selects a bounded, frozen overlay W(x)
```

长期产物按语义分为三路：可验证事实/明确偏好进入 Semantic Memory，重复稳定动作进入 Procedural
Skill，检索/上下文/编排/输出经验进入 Harness Policy。Hermes 继续独占原生 Memory/Skill 文件；
HermesGraph 的经验层只关联脱敏 native ChangeSet，不能复制、重放或覆盖原生写入。

Pattern 可以在 D1-D6 typed schema 中表达候选，但第一版在线 consumer 只开放
`capsule_memory_limit`、`memory_min_confidence`、`retrieval_profile`、`max_subqueries`、
`max_retrieval_rounds` 和 `graph_hops`。generation/output、token/字符预算、private quota、
source diversity 与 graph follow-up 在执行语义统一前只能离线评估或 Shadow。任何 Overlay 都不能
增加 capability、扩大 tenant/project/user scope、突破部署 hard cap、降低 citation/claim-support
门槛或绕过 `AnswerPublisher`。

Pattern 定义不可变，有效状态由 Postgres v14 append-only transition ledger 推导。每次评估依次冻结
`HarnessPatternEvaluation`、`HarnessPatternPromotionEvidence` 和 `HarnessPatternTransition`；
required case 失败不能被平均分掩盖。Draft 到 Shadow 可自动顺序晋级，Canary/Active 需要人工批准。
Run start 生成稳定 Canary bucket 和 exact `RunExecutionPolicy`，同时写入 `RunContext` 与
`RunSnapshot`；Observe/Shadow 的 `behavior_applied` 永远为 false。

完整数据模型、Postgres v12/v13/v14、deterministic diagnosis、E+/E- 检索、Pattern 状态机、API、迁移、
测试矩阵、SLO 和 `MH-001` 至 `MH-020` 实施清单见
[MemoHarness 固定化记忆与 Harness 自适应实施规划](./MEMOHARNESS_MEMORY_CONSOLIDATION_PLAN.md)。
当前已实现不可变 Experience/Evaluation、D1-D6、durable stage/reconciliation/backfill、
deterministic Pattern Draft miner、E+/E- selector、Postgres v12/v13/v14、Pattern evaluator、
Promotion Evidence、transition ledger、run-scoped overlay identity/hash、稳定 Canary 分桶和
capsule/retrieval/graph bounded consumer。生产回填为 33 Experience、33 Evaluation，二次新增 0、
冲突 0；真实样本未达到稳定阈值，所以生产仍为 0 Draft Pattern，没有 Pattern 被伪造晋级。
Canary health 与 auto rollback 属于 MH-015，尚未实现，因此当前不能宣称无人监督的在线策略闭环已成熟。

### 10.12 Personal Control Plane（已实现）

个人连续性使用独立 `PersonalControlService`，不把任务、人格或日归档塞入 provenance Memory：

```text
Hermes personal tool / FastAPI
  -> PersonalControlService
  -> scoped record + optimistic version
  -> JSON atomic document | Postgres v11/v15 personal_records
  -> append-only personal_events
  -> bounded personal capsule frozen at run start
```

领域对象包括 Task、Plan、PlanStep、ChecklistItem、Note、PersonaProfile、DayArchive、
EmotionOverride 和 TaskReminderState。生产 Compose 复用 Postgres migration runner；v11 创建通用
personal record/event 表，v15 扩展 reminder state 类型。Local/offline 使用原子 JSON，保持无外部
数据库可运行。

Hermes 通过 `manage_personal_tasks`、`manage_personal_plans`、`manage_personal_notes` 和
`correct_personal_memory` 调用服务，继续经过 Bridge 的 run scope、总调用预算、独立 personal
预算和 ToolEvent。Persona、Emotion、最多 8 个开放任务与 3 个活动计划进入
`RuntimeCapsuleProvider`；Emotion 是确定性 style-only reducer，不能改变工具选择、事实、证据或
权限。Day Archive 和自然语言 Memory correction 都有无模型实现。

工作台的 `QuickCapture` 不创建第二套事务仓。任务直接写 `PersonalTask`；“日程”同样写
`PersonalTask`，以 `due_at` 表示日期时间并附加 `scheduled` 标签；“笔记”写带 `note_date` 的
daily `PersonalNote`。保存响应中的 record ID 或日期只进入 `App` 的临时跨视图 focus state，
`ActionsView` 和 `ReviewView` 随后仍从 scoped Personal API 读取真实记录。因此刷新不会依赖前端
缓存维持事实，跨页面定位也不会复制服务端状态。

`ReviewView` 按 Persona 本地日期将 `due_at` 投影到月历，日期格只显示聚合数量，选中日期后读取
当天任务和 daily Note。完成任务继续携带 `expected_version`，保持乐观并发语义。`seal_day(date)`
除 trajectory、已完成任务和 open loops 外，也读取同日期 daily Note，并将笔记标题/正文纳入
summary、diary 与 bounded highlights。该聚合是确定性的，不调用模型。

顶部通知中心同样只投影现有 Task，不创建 Event 或 Reminder 事实表。服务端按 Persona IANA 时区将
开放任务分为 `overdue / due_soon / today`；仅持久化当前 `task_id + due_at + kind` 的已读和
`snoozed_until` 状态。任务改期、从 today 进入 due-soon 或进一步逾期时会形成新的未读提醒；完成或
归档后自动退出提醒流。前端每 60 秒轻量轮询 scoped API，并在页面恢复可见时刷新；浏览器桌面通知
必须由用户点击授权，应用关闭后的独立后台调度不在当前范围。

完整状态机、API、UI、迁移、纠错语义和验证见
[Personal Control Plane](./PERSONAL_CONTROL_PLANE.md)。

## 11. 评测体系

### 11.0 当前图谱抽取门禁（已实现）

`GraphExtractionGoldenSet` 是严格、版本化的输入资产。每个 case 包含来源 ID/标题/URI、domain pack、category、difficulty、tags、一个或多个原始 chunk、期望实体、可接受实体类型、实体别名、期望关系、可接受谓词和正确 evidence chunk index。来源三元组必须成组出现，证据索引不得越界，关系端点必须存在于期望实体，case ID 全集必须唯一。`required_pass=true` 用于提示注入、越权、负样本等不能被总体平均分掩盖的关键 case。

`GraphExtractionEvaluator` 顺序运行 extractor 以便准确归因单 case usage/latency，并执行：

```text
strict golden JSON
  -> deterministic eval Document/Chunk UUID5
  -> rule | OpenAI | hybrid extractor
  -> scope + pending hard gate
  -> one-to-one entity canonical/alias matching
  -> entity type accuracy
  -> relation endpoint/alias + predicate matching
  -> source chunk evidence validation
  -> per-case result + micro aggregate
  -> category + difficulty + tag slices
  -> thresholds + required-case gate
  -> atomic immutable-style JSON report
```

实体识别和实体类型分开评分，避免“名称正确但类型错误”被吞入单一 F1。关系使用方向敏感的一对一匹配。extractor 失败既计入 success rate，也把该 case 全部期望实体/关系记为 false negative。scope violation、非 pending 输出和 required case 失败始终阻断，不受数值阈值影响。

报告至少包含 entity/relation precision、recall、F1、entity type accuracy、evidence accuracy、case success rate、P50/P95 latency、input/cached/output/total token、逐 case 来源/预测，以及 category/difficulty/tag 切片。价格不写死在代码；调用方传入 input/cached/output 每百万 token 单价后，报告同时冻结 `TokenPricing` 与 `estimated_cost_usd`，保证历史可解释。CLI 使用同目录临时文件、flush/fsync 和原子替换，长时间 live gate 不会留下被误读的半截 JSON。

CLI：

```bash
# 离线基线，不作为通过声明
python -m app.evaluation.graph_cli --mode rule --report-only

# 生产门禁；失败返回非零退出码
hermesgraph-eval-graph --mode openai \
  --output .data/evals/openai_graph_extraction.json
```

初始 `2026-07-15-v1` 数据集有 5 个 case，用于证明评测合同和门禁行为。自然计算机集 `2026-07-16-v1` 有 18 个 case、14 个 arXiv 来源，覆盖 6 个 category、3 个 difficulty、自然负例、提示注入和多 Chunk 证据。当前 `openai-graph-extraction-v6-window-map-reduce:c6000:n4:o1:gpt-5.6-sol` 在 5-case 合同集和自然集上分别达到 5/5 与 18/18，实体、关系、类型和 evidence 指标均为 1.0；报告冻结 8,165 与 28,940 total tokens。该门禁只批准 v6 继续生成 pending candidate，不授权自动晋级或外推到 528 篇完整 PDF。扩展时必须更新 revision，不可原地篡改已出报告对应的数据；按 DomainPack 继续加入真实失败样本、边界样本、跨语言别名和 hard negative。

### 11.0.1 当前视觉抽取门禁（已实现）

`VisionGoldenSet` 对每个图片 case 固定 asset 相对路径与 SHA-256、来源合同、category、difficulty、
tags、required title/summary/OCR/warning、forbidden title/summary/OCR、期望区域类别/文本/框与
最大区域数。路径穿越、未知格式、不完整来源三元组、重复 region ID、空术语和越界期望在模型
调用前失败。提示注入与近空白负例使用 `required_pass`，不允许被其他图片的平均分抵消。

```text
strict golden JSON + frozen image hash
  -> Responses Vision + strict VisionAnalysis
  -> title / summary / top-level OCR / warning checks
  -> one-to-one region matching
  -> region category + self-contained text + bbox IoU
  -> forbidden-content + region-budget hard checks
  -> per-case usage / latency / retry history
  -> category + difficulty + tag slices
  -> thresholds + required-case gate
  -> fsync + atomic JSON report
```

区域匹配用术语、可见文字、类别与 IoU 选择唯一预测区域，额外区域不直接记 false positive，但超过
case budget 会阻断。模型异常把该 case 的全部期望项计为 false negative。CLI 默认允许在 SDK 重试
耗尽后，对连接、timeout、429 和 5xx 再做一次样本级恢复；`attempt_errors`、总 model attempts 和
recovered cases 全部留在报告中，数据/哈希/schema/语义失败不重试。`--case-id` 可重复指定，子集
revision 带选择集合的稳定哈希，避免把昂贵探针误认成完整门禁。

当前 `2026-07-16-v4` 有 11 个 case、13 个区域、8 个合成压力资产和 3 张真实 arXiv 页；`v1-v3`
按历史报告原样保留。最终 `openai-vision-knowledge-v3:gpt-5.6-sol` 为 11/11 调用成功、10/11 严格
case，summary term recall `0.9773`，其他质量指标全为 `1.0`，required 注入/空白 case 均通过；
P50/P95 `15.62s/74.62s`，39,538 total tokens，11 次模型尝试且 0 次恢复。该结果批准当前图片
派生抽取器用于后台入库，不代表 PDF 自动选页、开放世界照片、视觉 embedding 或跨页图表已完成。

```bash
hermesgraph-eval-vision \
  --dataset examples/evaluation/vision_golden.json \
  --output .data/evals/vision_openai.json
```

### 11.0.2 当前 Web Search 门禁（已实现，live provider 待通过）

`WebSearchGoldenSet` 是严格、版本化输入，case 固定 execution mode、预期 outcome/error code、
domain allow/primary/forbidden policy、术语组、来源数量、观测时效、难度、标签与 required gate。
当前 `2026-07-19-v1` 共 13 个 case：7 个真实 hosted-search case 与 6 个 required contract case，
覆盖 freshness、一手来源、URL citation、domain policy、密钥阻断、私网 URL、无引用、提示注入、
冲突来源、timeout、5xx 和中英文查询。

```text
versioned golden JSON
  -> select all | live | contract + stable subset revision
  -> pre-transport query policy or hosted Responses web_search
  -> public URL + run-scoped untrusted provenance validation
  -> citation/source/primary/term/observation-freshness metrics
  -> provider-only success + P50/P95 + usage/cost
  -> category/difficulty/tag slices + required-case gate
  -> fsync + atomic redacted JSON report
```

contract backend 惰性建连，在 `WEB_SEARCH_MODE=disabled` 且没有 API key 的清空环境中也可运行；
policy probe 直接调用与生产 adapter 相同的 pre-transport validator，确定性 fixture 不调用网络。
timeout/5xx fixture 只验证错误分类和降级合同，不进入 live provider 成功率。报告只保存 query
SHA-256、公开 source domains、错误类型/标准码/HTTP status、计数、usage 和延迟，不保存原始 query、
provider 错误正文或网页内容。401/403/404/429/5xx、timeout 与 connection error 分开归类，只有
timeout、429、5xx 和 connection error 允许有界样本级重试。

```bash
# 完全离线、无 key 的 required 安全/故障合同
hermesgraph-eval-web-search --execution contract

# 真实 provider 质量；失败默认返回非零退出码
hermesgraph-eval-web-search --execution live \
  --output .data/evals/web_search_live.json
```

当前 contract 报告 6/6 通过，policy/resilience/citation contract 均为 1.0。当前兼容端点的
`openai_current_web_search_docs` 最小 live probe 两次均为 HTTP 503，provider-only success 为 0，
所以没有运行或宣称其余 6 个 live case 已通过。历史单次 citation 成功仅保留为适配器纵向事实，
不能替代当前版本化 live gate。

### 11.1 数据集层次

```text
retrieval_cases       查询 -> 相关 chunk / entity / path
claim_cases            claim -> 支持/冲突/不足
answer_cases           问题 -> 结构化答案与引用要求
trajectory_cases       问题 -> 合理工具顺序和停止条件
security_cases         注入、越权、泄露、恶意技能
learning_cases         轨迹集合 -> 技能候选和回归结果
```

### 11.2 指标

- 检索：Recall@k、MRR、nDCG、实体链接准确率、路径命中率。
- 生成：faithfulness、answer relevance、citation precision、citation coverage。
- Agent：task completion、tool success、平均工具调用数、重复调用率、最大轮次触发率。
- 学习：技能复用率、技能带来的成功率提升、回归率、回滚率。
- 系统：p50/p95 latency、token/cost、Qdrant/Neo4j 查询耗时、队列积压。

### 11.3 评测流程

1. 所有 PR 先运行 schema、unit、contract tests。
2. retrieval pipeline 运行固定检索集。
3. agent replay 使用固定模型配置、固定知识库快照和固定工具结果。
4. evaluator 输出 JSON，不以自然语言评审结果作为唯一依据。
5. 新技能与 baseline 并行运行，生成 diff 报告。
6. 只有门禁通过，才允许进入 shadow/canary。

## 12. 可观测性

### 12.1 Trace 结构

```text
trace
└── agent_run
    ├── query_understanding
    ├── agent_turn_1
    │   ├── tool.search_hybrid
    │   └── tool.search_graph
    ├── agent_turn_2
    │   ├── tool.fetch_evidence
    │   └── tool.verify_evidence
    ├── evidence_audit
    ├── answer_generation
    └── learning_postprocess
```

Hermes sidecar 提供 run 状态与事件流；受信任插件把 HermesGraph capability 调用和原生
`memory/skill_manage` hook 关联到应用 `run_id`。项目 trace adapter 记录 bridge run、Hermes run、
工具 hash、证据、发布结果和错误类型，并与租户、项目和知识库版本关联。生产环境默认关闭敏感原文
采集，只保存 hash、长度、字段类型和必要的审计摘要；原生学习正文只进入受权限控制的审计资产。

### 12.2 必备 metrics

```text
agent_run_total{task_type,status}
agent_run_duration_seconds{task_type}
agent_tool_calls_total{tool,status}
retrieval_latency_seconds{mode}
retrieval_recall_at_k{dataset}
answer_citation_coverage
answer_unsupported_claim_rate
memory_write_total{memory_type,status}
skill_promotion_total{from_status,to_status}
skill_regression_total
llm_tokens_total{model,direction}
```

日志使用 JSON，必须带 `request_id`、`run_id`、`trace_id`、`tenant_id`、`project_id`、`tool_name`、`asset_version`。禁止把 API key、完整 Authorization header 和未经脱敏的用户内容写入日志。

## 13. API 设计

### 13.1 对话

`POST /v1/projects/{project_id}/runs`

请求：

```json
{
  "session_id": "session-001",
  "input": "比较 GraphRAG 和普通向量 RAG 在多跳问题上的差异",
  "mode": "stream",
  "options": {
    "max_turns": 10,
    "include_graph": true,
    "citation_level": "claim"
  }
}
```

响应事件：

```text
run.started
plan.created
tool.started
tool.completed
evidence.added
claim.updated
answer.delta
answer.completed
run.completed
```

### 13.2 文档

- `POST /v1/projects/{id}/documents`：创建上传任务。
- `POST /v1/projects/{id}/documents/ingest`：提交 URL、Sitemap 或 Git 来源。
- `GET /v1/projects/{id}/documents/{document_id}`：查看版本和处理状态。
- `POST /v1/documents/{document_id}/reprocess`：指定 parser/embedding revision 重处理。
- `DELETE /v1/documents/{document_id}`：软删除并异步清理索引。

### 13.3 评测和学习

- `GET /v1/projects/{id}/runs/{run_id}/trace`
- `POST /v1/projects/{id}/runs/{run_id}/feedback`
- `GET /v1/projects/{id}/skills`
- `POST /v1/projects/{id}/skills/{skill_id}/promote`
- `POST /v1/projects/{id}/evals/run`
- `POST /v1/projects/{id}/memories/{memory_id}/revoke`

所有管理接口要经过 RBAC 和审计；技能发布接口必须携带 `expected_parent_version`，防止并发覆盖。

### 13.4 当前图谱检索与审核 API

- `POST /v1/projects/{id}/graph/search`：只接受 allowlisted template、实体列表、hop/limit，不接受自由 Cypher。
- `POST /v1/projects/{id}/graph/entities/resolve`：canonical/alias/type 实体解析与来源证据。
- `POST /v1/projects/{id}/graph/retrieve`：文本证据与多跳子图联合检索。
- `POST /v1/projects/{id}/graph/compare`：连接路径、共享邻居与左右独有邻居。
- `GET /v1/projects/{id}/graph/candidates`：按 `document_id` 和状态读取实体、关系、归并三类候选。
- `POST /v1/projects/{id}/graph/candidates/entities/{candidate_id}/review`
- `POST /v1/projects/{id}/graph/candidates/relations/{candidate_id}/review`
- `POST /v1/projects/{id}/graph/candidates/resolutions/{candidate_id}/review`

审核请求只允许状态机定义的目标状态，并记录 `reviewer_id` 与原因。API 返回 404 表示候选不在当前 project scope，409 表示状态迁移或端点约束不满足。前端不能直接修改 JSON 审计仓或 Neo4j 状态。

## 14. 安全设计

### 14.1 威胁模型

- 文档/网页中的 prompt injection。
- 恶意或错误技能供应链。
- 记忆污染和跨租户召回。
- 任意 Cypher、SQL、Shell 和外部网络写入。
- trace、引用和导出物泄露敏感数据。
- Agent 循环导致资源耗尽。

### 14.2 控制措施

1. 所有检索结果以 `untrusted_evidence` 传入 Agent，不能覆盖系统指令。
2. 工具采用 allowlist；Neo4j 使用模板参数，不允许自由 Cypher。
3. 读工具和写工具分离不同服务账号；P0 只启用读工具。
4. 每次运行绑定 tenant/project filter，数据库层再做一次隔离检查。
5. 使用 max turns、max tool calls、token budget、timeout、concurrency cap。
6. 外部 URL 解析使用网络出口策略、大小限制、MIME allowlist 和病毒扫描。
7. 导出文件、trace 和 memory 支持租户级保留期和删除操作。
8. 高风险写操作采用 human-in-the-loop，审批前后都运行输入 guardrail。
9. Hermes API、Capability Bridge 与 native admin 使用三份独立 secret；生产/预发布各不少于 32
   字符，比较使用 constant-time，日志和错误响应不得输出 token。native admin `8643` 只在 Compose
   网络内可达，不做宿主端口映射。
10. Hermes 只启用原生 `memory/skills/todo` 和受信任 `hermesgraph-bridge`；terminal、file、browser、
    delegation、session search 与 Hermes 原生 web 默认关闭。`terminal.backend` 仍强制为没有挂载
    Docker socket 的 `docker`，即使未来误开 terminal tool 也不能落回 sidecar 本地执行。sidecar 以
    非 root 运行且宿主端口只绑定 `127.0.0.1`。
11. bridge ID 不含作用域信息且不可预测；稳定 session key 只保存 HMAC，模型和 sidecar 不接收原始
    tenant/project/user。bridge 状态完成后保留 10 分钟以接收后台原生学习审计，再自动清理。
12. 原生学习快照路径固定在 `HERMES_HOME`，拒绝 symlink/path traversal/特殊文件；回滚必须同时校验
    snapshot 状态、expected after hash、当前目标 hash 和目标互斥锁。

## 15. 可靠性与并发

### 15.1 幂等

- 当前文件 ingestion 使用 `tenant_id + project_id + content_hash` 合并活跃 job，文档与 chunk 使用 scope/content hash/ordinal 的稳定 UUID；未来 URL connector 再加入 canonical URI 和 parser version。
- learning job 使用 `SHA-256(trigger + canonical trajectory snapshot)` 合并重复提交；feedback snapshot 变化会产生新 generation。
- learning effect 使用稳定逻辑身份与 payload hash：Memory scope/type/key、Skill name/version/definition hash、Evaluation 输入指纹、Observation skill/run/cohort/evaluator revision、ChangeSet mutation hash。
- vector upsert 使用稳定的 `chunk_id` 作为 point ID。
- graph MERGE 使用稳定 canonical key。
- outbox 事件带 `event_id`，消费者记录去重表。
- skill promotion 使用乐观锁。

### 15.2 重试

只对网络错误、429、临时数据库错误和可恢复解析错误重试；模型输出 schema 错误最多重试一次；权限错误、数据损坏和恶意内容直接进入人工处理队列。指数退避并设置上限，避免重试风暴。

### 15.3 长任务

当前 ingestion 与 learning 都不依赖 Redis broker：Postgres 表同时承担 durable 状态和领取队列，worker 使用 `SKIP LOCKED`、lease 与 heartbeat 支持跨进程恢复，learning 额外使用 fencing token 阻断过期执行确认。若后续需要人工审批等待、数小时级综述或定时任务，再接入 Temporal/Dapr/DBOS。durable orchestrator 只负责任务状态和恢复，在线 Agent Loop 仍由 Hermes sidecar 执行。

## 16. 测试策略

### 16.1 单元测试

- chunk boundary、metadata、hash、版本选择。
- RRF、dedup、filter、score normalization。
- Cypher template 参数、limit、hops 和租户过滤。
- claim/citation coverage 和 conflict classification。
- memory write gate、skill parser、promotion rule。

### 16.2 Contract tests

- Qdrant adapter 在 mock 与真实容器上返回相同 `RetrievalBundle` schema。
- Neo4j adapter 的所有模板均有最小图谱 fixture。
- Hermes plugin tool、内部 bridge endpoint 与严格发布输入输出符合 Pydantic contract。
- Hermes session HMAC、三 token 认证、conversation history、首发不可变、重复发布幂等、无发布终态、
  background review 父 session 关联、completion 握手和延迟 bridge 释放全部有 contract。
- 原生 Memory/Skill 覆盖 file/directory/new-asset 快照、staged/no-op、target drift、幂等回滚和
  append-only review ledger、accepted retention、安全 GC、容量 fail-closed 与 admin 鉴权。
- OpenAI graph structured output、usage observer 与 golden evaluator 符合 Pydantic/scope/pending/evidence contract。
- SSE 事件顺序符合 `started -> progress -> completed/failed`。

### 16.3 集成测试

当前使用 Docker Compose 启动 FastAPI、Hermes Agent、Postgres、Qdrant 和 Neo4j，验证 sidecar 健康、
插件启用、toolset allowlist、native admin 内网鉴权、migration、并发提交合并、worker claim/lease、
上传、两套索引、图候选、POST SSE、引用、学习变更和归档。Postgres
knowledge/outbox/learning artifact adapter 的真实合同已通过；v1-v10 无损升级、旧学习资产一次性导入、
真实 Agent checkpoint、transition 拒绝审计、stage 异常回滚、提交前 fencing token 变化、Skill 状态与
ledger 原子回滚、link 修复、精确 Skill version 丢失检测、进程重启和 API worker + 独立 worker
双进程竞争均已验收。当前 324 个测试 collected，307 passed、17 个环境型 skip；Ruff 全绿，162 个
应用源码 strict mypy 通过；16 项真实 PostgreSQL adapter contract 全部通过。Hermes 0.19 真实
Agent 首发、主 run 正常收尾和 background review 已
完成；最终 completion 握手 live 重验受 `429 model_cooldown` 阻断，contract 已覆盖但仍不能替代
provider 恢复后的纵向重验。v6 图谱小型门禁已通过，但共享网关在
20-document pilot 持续 timeout；这不阻塞离线/容器后端能力交付。Redis 与 MinIO 不属于当前运行时依赖；S3 对象存储和逐
stage 操作系统级强杀的扩展故障矩阵仍待加入。

### 16.4 安全测试

- 在 PDF、网页、chunk、memory、skill 中注入指令，确认不改变系统策略。
- 尝试跨 project 检索和跨租户引用。
- 尝试自由 Cypher、SQL、Shell、任意外部 HTTP。
- 技能中加入隐藏外传指令，确认无法通过安全门禁。
- 检查日志、trace、导出物和错误消息是否泄露秘密。

## 17. 本地基础设施

### 17.1 Docker Compose 服务

```text
app: HermesGraph FastAPI + built React workbench
hermes: hermes-agent 0.19.0 API sidecar + trusted hermesgraph-bridge plugin
postgres:17.10-alpine3.23
qdrant/qdrant:v1.18.1
neo4j:2026.06.0
app_data, hermes_data, postgres_data, qdrant_data, neo4j_data, neo4j_logs
```

宿主公开端口只绑定 `127.0.0.1`。应用以 UID 10001 非 root 用户运行；Hermes sidecar 也以非 root
用户运行，固定 `hermes-agent==0.19.0` 与基础镜像 digest。native admin `8643` 只 expose 给 Compose
网络，没有 `ports` 映射。`scripts/docker_up.sh` 先构建前端静态产物，再执行
`docker compose up --build -d`。Compose 默认 `RUNTIME_MODE=hermes`，检索
planner/embedding/graph extractor 可保持 deterministic/rule；生产通过 `.env` 注入模型、embedding、
Hermes API key、独立 bridge token 和独立 native admin token。生产/预发布的三份 Hermes secret 必须
各自至少 32 字符且不能复用。

生产环境还要把镜像 tag 锁成组织批准的 digest，Postgres/Neo4j 使用最小权限账号并轮换密码，所有数据卷做备份恢复演练，collection/schema 变更走迁移。当前 Compose 的默认密码只允许本机开发。

### 17.2 常用命令

```bash
./scripts/docker_up.sh
docker compose ps
docker compose logs -f app
docker compose logs -f hermes
./.venv/bin/python scripts/infrastructure_smoke.py
./.venv/bin/pytest -q
./.venv/bin/ruff check app tests scripts
./.venv/bin/mypy app
```

## 18. 分阶段实施清单

### Step 1：仓库和基础配置

创建目录、Python 环境、依赖 lock、配置模型、日志和错误合同。

完成条件：API health、配置校验、ruff/mypy/pytest 可运行。

### Step 2：本地基础设施

启动 Postgres、Qdrant、Neo4j；执行 migration、索引和约束初始化。Redis、MinIO 在明确需要 broker/cache 或对象存储时再加入。

完成条件：每个服务有 health check，数据可持久化，测试可清理。

### Step 3：文档入库最小链路

实现 PDF/Markdown loader、解析、切分、content hash、chunk 表和对象存储。

完成条件：同一文件重复提交幂等，chunk 可定位到页码/段落。

当前状态：本地 raw object、Postgres durable job、Postgres chunk/metadata repository、共享 checksum migration 和事务 outbox 已实现；worker lease、自动重试、取消、并发 hash 合并、真实 Postgres adapter contract、Docker 旧数据导入与重启均已通过。S3 对象存储和版本化增量索引留到后续生产适配阶段。

### Step 4：Qdrant Hybrid Retrieval

实现 dense/sparse embedding adapter、collection 初始化、payload indexes、RRF、过滤和 rerank 接口。

完成条件：检索合同稳定，离线数据集输出 Recall@20 和 MRR。

当前状态：adapter、collection/schema、filtered dense+sparse RRF、Qdrant sparse IDF、标题+正文共同编码、来源级候选多样化、归档、内存 contract 和真实容器纵向测试已完成。其上已增加 `AgenticRetrievalController`：严格 OpenAI/确定性 planner、稳定比较分解、显式个人/视觉/比较意图约束、简单查询原文锚定、模型改写 fallback、最多 4 子查询/2 轮、LangChain `abatch`、跨查询 RRF、gap assessment、partial failure、scope 二次隔离和完整 decision trace。当前 528 篇/43,872 chunks 加 31 个个人 chunks 的 IDF collection 已通过 57/57、Recall@20 1.0、MRR 0.8924、P95 34 ms 的 deterministic 全量门禁。生产 embedding 隔离重建、usage、可选成本、baseline diff 和 MRR 防退化门槛已经实现，但当前兼容 Key 不提供 embedding 模型，live gate 仍未通过；学习型 reranker 待完成。

### Step 5：Neo4j 图谱入库

实现实体/关系 schema、entity resolver、MERGE、来源绑定和五个 Cypher 模板。

完成条件：方法-数据集-指标路径可查询，所有节点/边都能追溯来源。

当前状态：结构化 Document/Chunk MERGE、规则/OpenAI/混合实体关系候选、稳定 ID、v2 JSON 审计仓、确定性跨文档 resolver、Neo4j candidate/`same_as` projection、人工晋级/拒绝、级联撤销、来源绑定、归档、结构 revision 重投影、候选证据对账和三个通用 allowlisted traversal 已完成。OpenAI extractor v6 的合同/自然 arXiv live gate 为 5/5、18/18、全指标 1.0，并冻结 usage/latency 与切片报告；528 篇全量模型 backfill 因共享网关 pilot timeout 尚未执行，完整 PDF 隐式别名/歧义消解、开放领域本体和更丰富的方法-数据集-指标模板仍待扩展。

### Step 6：Evidence Service

实现 fetch、claim schema、support/contradiction/insufficient 分类和 citation coverage。

完成条件：给定 claim 和 evidence，结果确定且可单元测试。

### Step 7：Hermes Agent runtime 与 Capability Bridge

实现固定版本 Hermes sidecar、`HermesAgentRuntime` adapter、受信任插件、三 token 认证、HMAC
session scope、最大轮次、工具预算、严格发布、trace/native learning hook 和精确快照回滚。

完成条件：lookup/compare 两类问题可以真实完成并产出结构化 `AnswerResponse`。

当前状态：Hermes 0.19.0 镜像、持久 profile、sidecar 健康检查、插件显式启用、原生
memory/skills/todo toolset、`/v1/runs` HTTP/SSE adapter、run-scoped bridge、bounded history、
严格 `AgentAnswerDraft`、服务端 citation hydration、首发不可变/重复发布幂等、总量/分工具预算、
取消、approval 拒绝、
native write 快照、脱敏 ChangeSet、审核、条件回滚、retention/GC 和容量健康已完成。OpenAI Agents SDK
runtime、session adapter 和依赖已经删除。当前五服务 Compose、插件/toolset 和未映射宿主的 native admin
`8643` 已实机验证。真实 Agent 回合在首次 publish 后约 12 秒向用户返回，Hermes 主 run 随后正常
completed，并启动 `bg-review` 调用 memory/skills。0.19 没有 plugin `return_direct` 语义，因此项目
采用正常 finalizer，不用 `/stop` 截断；review 用父 `session_id` 审计并通过 `on_session_end` completion
event 释放 bridge。最终握手部署后的 live 重验遇到上游 `429 model_cooldown`，该错误发生在模型
网关，不是 Hermes API、Capability Bridge、native admin、Neo4j 或 Qdrant；provider 恢复后需补跑。

### Step 8：LangChain Integration Runtime

用 LangChain Document、Prompt、retriever、RunnableParallel/Branch、callbacks 和工具适配器组合中间能力，由 HermesGraph Capability Bridge 暴露给 Hermes 插件。

完成条件：检索、图谱、Prompt 和学习预处理均可脱离 Agent 单独运行；在线 Agent 不依赖 LangChain Agent。

### Step 9：评测与回放

建立 golden cases、retrieval cases、security cases 和可重复 replay runner。

完成条件：每次 prompt、策略、技能或依赖变更都有 baseline diff。

当前状态：回答级 replay、citation metrics，以及图谱抽取 5-case 合同集和 18-case/14-source 自然 arXiv 集、required 安全门禁、micro/slice metrics、usage/cost report 和原子 CLI 已完成；图谱 OpenAI v6 两套 live report 均通过，全质量指标 1.0。视觉侧已增加 11-case/13-region、3 张真实 arXiv 页、required 注入/空白门禁、区域 OCR/类别/IoU、usage/latency/retry/slice 报告；最终 Vision v3 为 11/11 调用成功、10/11 strict case，summary `0.9773`，其他质量指标 `1.0`。检索侧同时有 5-case contract fixture 和 57-case 自然 arXiv/个人/Qdrant gate，报告包含 Recall@K、MRR、forbidden/root source、intent、模型计划、轮次、新增证据、distinct source、category/difficulty 切片、mean/p95 latency、planner token usage、停止原因和 fallback，并以原子 JSON 落盘。Vision 补页后的 528 篇全量报告为 57/57、Recall@20 1.0、MRR 0.8924、P95 34 ms，见 `.data/evaluations/arxiv_retrieval_v4_vision_complete.json`。embedding 校准器已实现自动 baseline diff、逐 case 回归清单和 MRR 最大下降门槛。生产 embedding live 报告因当前 Key 无 embedding 模型待完成。

### Step 10：记忆系统

实现 episodic trace、semantic memory、memory write gate、来源绑定、撤回和过期。

完成条件：能检索历史任务，同时能证明跨 tenant、低信任记忆不会进入高置信回答。

### Step 11：技能学习闭环

实现 reflection、skill candidate、YAML schema、离线回放、shadow、canary 和 rollback。

当前状态：首条闭环、Postgres durable learning execution、v6 learning artifact audit repository、v7 reflection/stage checkpoint、v8 Skill transition ledger、v9 同事务 stage commit/artifact reconciliation、v10 精确版本 link，以及冻结能力 counterfactual replay 和跨版本 SemVer refinement 已实现。重复成功轨迹可自动生成系统评测并进入 Shadow；已观测版本可派生新的 Draft，Canary/Active 需要健康样本与人工批准，退化自动回滚。后续增强是开放分布 replay 固定集和真实 provider/tool 仿真。

### Step 12：生产化

加入 RBAC、审批、限流、预算、任务恢复、备份、告警、数据删除和部署清单。

完成条件：故障演练、恢复演练、安全测试和性能测试通过。

## 19. 第一条端到端开发路径

建议第一次不要直接实现自学习。按下面的最短闭环完成第一条可运行链路：

```text
5 篇论文
  -> PDF 解析
  -> chunks
  -> Qdrant dense/sparse
  -> Neo4j Method/Dataset/Metric
  -> search_hybrid + search_graph
  -> fetch_evidence
  -> verify_evidence
  -> Hermes Agent calls hermesgraph_publish_answer
  -> trace + golden eval
```

这条链路稳定后，再加入技能和记忆。否则一开始同时引入自学习，会把“检索失败”“抽取失败”“Agent 规划失败”和“技能污染”混在一起，无法定位质量问题。

## 20. 关键伪代码

```python
async def answer(project_id: str, session_id: str, user_input: str) -> AnswerResponse:
    run = await run_repo.create(project_id, session_id, user_input)
    context = RunContext(run_id=run.id, project_id=project_id)

    bridge_id = await hermes_bridge.open_run(context)
    hermes_run = await hermes_api.create_run(
        input=user_input,
        instructions=load_hermes_instructions(context),
        session_id=bridge_id,
        session_key=hmac_scope(context),
    )
    await hermes_api.wait_terminal(hermes_run.id)
    response = await hermes_bridge.published_answer(bridge_id)
    await run_repo.complete(run.id, response)
    await outbox.publish("run.completed", {"run_id": str(run.id)})
    return response
```

学习 Worker：

```python
async def process_completed_run(run_id: str) -> None:
    trace = await trace_repo.load(run_id)
    outcome = await learning_engine.process_completed_run(trace)
    await skill_evolution.observe_run(trace)

    if outcome.skill_candidate and settings.learning_mode != "observe":
        result = await skill_evolution.evaluate_and_stage(outcome.skill_candidate.skill_id)
        assert result.skill.status in {SkillStatus.DRAFT, SkillStatus.SHADOW}
```

## 21. 需要保留的架构决策记录

### ADR-010：Agent 使用语义化 GraphRAG 工具，不生成 Cypher

Hermes 只看到实体解析、证据子图、实体对比和固定模板遍历四类工具。Cypher 深度在代码中预编译，
scope 来自 `RunContext`，关系必须 join 到 Chunk evidence；`GraphRetrievalToolkit` 在返回 Agent 前再次
执行 scope/evidence 复核。这样既保留 Agent 对检索策略的选择权，又不把数据库查询语言和隔离边界
交给模型。详见 [`ADR-010`](./ADR-010-graphrag-tools.md)。

### ADR-009：删除 OpenAI Agents SDK fallback

原因：Hermes sidecar、bridge、严格发布和原生学习治理已经形成完整单循环；继续保留第二运行时会扩大
依赖、配置和安全审计面，并允许架构重新漂移。OpenAI Python SDK 继续作为模型原语客户端。
详见 [`ADR-009`](./ADR-009-remove-openai-agents-fallback.md)。

### ADR-008：Hermes Agent 是唯一在线 Agent Loop

原因：直接复用 Hermes 更成熟的个人会话、Memory、Skill、Todo 与后台回顾能力；HermesGraph 保留
run scope、私有知识、图谱、证据发布、评测和回滚治理。LangChain 继续作为 Integration Runtime，
OpenAI Python SDK 继续提供模型原语。其迁移期 fallback 条款已被 ADR-009 取代。详见
[`ADR-008`](./ADR-008-hermes-first-runtime.md)。

### ADR-005：自学习是控制平面，Skill 是版本化学习产物

原因：Agent Core 必须稳定；经验、反馈和评测由 Learning Control Plane 处理，可复用行为发布成声明式 Skill。在线 Agent 无权直接修改 active 资产。

### ADR-006：核心运行时领域无关

原因：当前领域未固定。实体 schema、图查询、输出合同和评测集由 DomainPack 提供，核心只依赖稳定接口。

### ADR-002：Qdrant 是主向量检索，Neo4j 是主关系图谱

原因：职责清晰。Neo4j 可有图内向量索引作为补充，但不能让两个数据库同时拥有不可解释的“主排名”。

### ADR-003：自学习默认改配置资产，不改模型权重和核心代码

原因：非参数化学习更可审计、可回放、可回滚，适合第一版建立可靠性。

### ADR-004：所有事实必须带来源链

原因：研究型产品的核心不是生成更长答案，而是知道答案从哪里来、证据是否支持、何时应该承认不知道。

## 22. 官方参考资料

- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/responses)
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Hermes Agent 官方文档](https://hermes-agent.nousresearch.com/docs/)
- [Hermes Agent 官方仓库](https://github.com/NousResearch/hermes-agent)
- [LangChain Tools](https://docs.langchain.com/oss/python/langchain/tools)
- [LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/structured-output)
- [Neo4j GraphRAG for Python](https://neo4j.com/docs/neo4j-graphrag-python/current/)
- [Neo4j GraphRAG RAG Guide](https://neo4j.com/docs/neo4j-graphrag-python/current/user_guide_rag.html)
- [Qdrant Hybrid Search](https://qdrant.tech/documentation/search/text-search/hybrid-search/)
- [Qdrant Hybrid Queries](https://qdrant.tech/documentation/search/hybrid-queries/)

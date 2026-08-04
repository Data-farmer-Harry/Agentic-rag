# HermesGraph Intent Lock

最后更新：2026-08-03

## 北极星目标

构建一个 **Hermes-first、OpenAI-powered、以研发团队为业务主线、同时支持个人学习的自进化
多模态 Agentic RAG 系统**。Hermes Agent 是唯一在线 Agent Runtime；
OpenAI Python SDK 提供 Responses、Vision、Structured Outputs、Embeddings 等模型能力；HermesGraph
提供团队/个人知识、Agentic RAG、知识图谱、证据发布、作用域隔离和学习审计。系统持续理解研发
文档、架构、API、服务、事故、决策、论文、图片、个人记忆和任务轨迹，主动规划、搜索、补充检索、
验证证据并回答，再从真实使用反馈中形成原生 Memory/Skill 与受治理的检索、图谱和策略改进。

它不是“带聊天框的向量检索”，也不是会无约束修改自己的自治程序。完成形态必须同时
具备团队与个人知识库、多模态理解、工具行动、证据引用、长期记忆和受控自进化。第一交付目标不是
继续堆叠后端能力，而是让研发用户在不理解 RAG、图谱或 Skill 内部机制的情况下完成真实工作。

## 2026-08-03 业务主线锁定

1. 商业主线固定为 **Engineering Intelligence Agent**：帮助 AI/软件研发团队查询内部知识、理解
   软件架构、追踪技术决策、复用事故经验并完成有引用的技术调研。
2. 通用能力必须保留。同一系统可作为个人学习 Agent 使用，支持论文、笔记、任务、长期记忆和
   学习工作流；团队模式与个人模式共享 Agent/RAG/Graph/Memory 核心，不复制两套产品。
3. 当前阶段以体验为最高优先级。先把首次使用、对话、上传、检索过程、引用、图谱路径、反馈、
   错误恢复和移动端体验做完整，再增加 GitHub、飞书、Jira、Confluence 等连接器。
4. 团队知识与个人补充知识必须在来源、信任和展示上可区分。历史 528 篇 arXiv 论文暂时不作为
   企业演示默认语料，后续作为个人/公共参考知识层按需启用，不能淹没团队内部资料。
5. 第一套业务验收语料使用完全虚构的企业研发知识库，包含架构、服务、API、ADR、事故、Runbook、
   安全策略、版本记录和组织归属；必须配套可机器判定的检索与回答问题集。
6. “完成”以用户任务闭环和真实浏览器验收为准，不以新增模块、接口数量或页面数量代替体验质量。

## 核心能力主线

1. `Responses API` 是文本、视觉、结构化抽取和复杂响应的首选模型 API。
2. `Tool Calling` 是 Agent 使用检索、知识图谱、记忆和受控外部能力的标准接口。
3. `Structured Outputs` 用于计划、实体关系、证据判断、学习候选和内部状态合同。
4. `Vision` 用于理解图片、截图、扫描件、图表和 PDF 页面视觉信息；原始媒体必须保留。
5. `Embeddings + Vector Search` 与稀疏检索、元数据过滤、重排和知识图谱共同组成检索层。
6. `Background Tasks` 只在长时摄取、重索引、评测或研究任务确有需要时启用，并保持可恢复状态。
7. Hermes Agent `0.19.0` 是当前唯一在线 Agent Loop，负责会话、工具循环、原生 Memory、Skill、
   Todo 和后台回顾；它通过受信任插件调用 HermesGraph，不直接连接数据库或自由执行 Shell。
8. OpenAI Agents SDK fallback 已删除；领域合同不依赖第二个 Agent SDK，同一次用户请求不得出现
   另一个相互竞争的 Agent Loop。OpenAI Python SDK 只提供模型 API，不拥有会话与工具循环。

## 不可漂移约束

1. Agentic RAG 必须支持 `理解问题 -> 分解/改写 -> 多源检索 -> 识别证据缺口 -> 补充检索
   -> 证据验证 -> 带引用回答`，不能退化成固定一次 `top_k` 检索。
2. LangChain 是 Integration Runtime，承担 loader、splitter、retriever、Runnable 数据流、
   结构化转换、重试和 callbacks；它不创建第二个在线 Agent，也不包裹 Hermes 循环。
3. Hermes 只能通过带认证、run scope、预算和证据白名单的 Capability Bridge 使用
   `search_knowledge`、`resolve_graph_entities`、`retrieve_evidence_subgraph`、
   `compare_graph_entities`、固定模板 `search_graph`、可选 `search_web`、
   `recall_project_memory`、`activate_governed_skill`、可选只读 Computer Workspace tools 和
   `hermesgraph_publish_answer`；tenant/project/user 作用域永远由应用服务端绑定，Agent 不能提交
   Cypher 或自行扩大本机路径 root。
4. 自进化默认是非参数化学习，不直接训练权重，不让模型修改核心代码、安全策略、图谱 active
   事实或外部写权限。Hermes 原生 Memory/Skill 可以在其持久 profile 中应用，但每次写入必须先保存
   精确快照，成功后镜像为脱敏 `requires_audit` ChangeSet；审查与回滚采用 append-only ledger，
   回滚必须满足当前内容仍等于 audited after hash。
5. 高影响学习资产继续执行 `trace -> feedback -> reflection -> candidate -> eval -> promotion`；
   检索策略、Prompt、图谱事实和 HermesGraph active Skill 不得绕过评测门禁。
6. Memory 至少包含 episodic、semantic、procedural、policy 四类，并带来源、作用域、
   信任等级、有效期和撤销状态。
7. Skill 使用渐进披露：discovery、activation、execution；只能调用声明过的受控工具。
8. 文本、图片和后续音频/视频都是一等 Knowledge Object。原件、派生文本、视觉区域、
   embedding、图谱候选和引用之间必须保留 provenance。
9. 产品默认面向研发团队工作区，同时支持个人私有工作区；tenant/project/user 隔离和数据删除能力
   必须从底层强制，不能因为本地单用户部署或个人模式而省略数据边界。
10. 第一个业务 DomainPack 固定为软件工程，覆盖 Service、Repository、Module、API、Team、Decision、
    Incident、Runbook 等研发对象；计算机/AI 论文作为个人与公共参考资料继续支持，核心合同仍允许
    以后增加其他领域。
11. OpenAI 是首选协议与模型能力来源；允许通过受控 OpenAI-compatible adapter 使用用户
    指定端点，但必须先通过协议探测和评测门禁。
12. 没有模型密钥和外部数据库时，项目仍能运行单元测试与 deterministic 离线演示；
    离线演示不能被描述为真实模型质量。
13. arXiv 是计算机公共参考知识的重要来源。同步必须遵守来源标识、增量游标、速率限制、
    下载预算、content hash 去重和失败恢复；论文元数据、PDF 原件、解析文本、图片页和引用
    必须可追溯，不能进行无资源上限的盲目抓取。
14. 当前变化或个人知识库之外的公开事实可以走受控 Web Search，但网络内容始终是
    `untrusted evidence`。联网查询必须有独立 scope、调用/结果/超时预算、敏感信息阻断、
    domain policy、URL citation 和运行级 provenance；无引用搜索结果不能支撑答案。
15. Durable learning 的 reflection 必须先形成不可变 checkpoint；后台 artifact 写入必须校验
    当前 job lease/fencing token。相同稳定 ID 的 Evaluation、Observation 和 ChangeSet 若语义
    payload 不同必须报冲突，不能覆盖；不得把外部模型调用宣称为严格 exactly-once。
16. Reflection 之后的确定性 learning stage 必须让 artifact、Skill 当前状态、transition ledger、
    artifact link 与 checkpoint 复用同一 Postgres transaction。派生 link 可以重建，artifact 本体
    不得由 reconciliation 猜测补写；账实不符必须进入 `required` 并保留错误原因。
17. Skill refinement 必须生成不可变父版本的 SemVer 子版本；评估、健康度、状态迁移、checkpoint、
    result 与 reconciliation 必须能指向精确 Skill version，不能用同一 `skill_id` 下的任意版本代替。
18. Skill 离线门禁必须在有界、只读、无外部副作用的沙箱中执行声明式步骤。冻结工具输出只作为
    fixture 使用，持久报告只保留 hash、错误码和指标；不得把历史输出原文写入评测报告。
19. Web Search 的确定性安全/故障合同与真实 provider 质量必须分开计分。固定评测资产需要版本号；
    报告不得持久化原始 query、凭据或网页正文。fixture 通过不能替代 live citation/source/freshness
    验收，也不能抬高 provider-only success rate。
20. 受治理 Skill 必须在 run start 冻结 discovery index，在线激活只能读取同一 snapshot 钉住的
    Canary/Active 精确版本；Skill 返回声明式步骤，不能执行任意脚本或授予新能力。
21. Computer Workspace 默认是显式 root、只读、scope-bound 的证据源。路径逃逸、隐藏/凭据文件、
    私钥格式和 symlink 必须失败关闭；本机写改删继续属于非目标。
22. MemoHarness 风格的经验固定化只能作为 HermesGraph 控制面：逐案例经验 `E` append-only，
    全局模式 `G` 必须版本化并经过离线、shadow、canary 门禁；单次运行只消费已批准模式形成
    run-scoped overlay，并在 run start 冻结。Hermes 继续独占原生 Memory/Skill 写入，经验层不能
    创建第二 Agent Loop、双写原生资产、扩大 capability/scope/budget 或降低证据门槛。
23. Agentic RAG 的固定定义是 `typed plan -> bounded parallel retrieval -> evidence requirement/gap
    assessment -> bounded repair -> evidence-constrained publish`。当前 v1 按
    `AGENTIC_RAG_LOCK.md` 冻结，只有用户明确恢复 `RAG-*` 工作项时才能改变检索策略。Hermes
    继续是唯一 Agent Loop；在生产 embedding、经审核 active 语义 KG、claim-evidence entailment
    和真实 Hermes 纵向门禁完成前，不得宣称生产级 Agentic GraphRAG。
24. Personal Control Plane 是 Task/Plan/Step/Checklist/Note、Persona/Onboarding、Day Archive、
    自然语言 Memory 纠错和 Emotion 的唯一结构化产品层。它必须保持 tenant/project/user scope、
    乐观版本、append-only event 和用户可编辑性。Persona/Emotion 只能调节表达，不能改变事实、
    证据、权限、安全判断或任务优先级；不得与 Hermes 原生 Memory/Skill/Todo 做无来源双写。
25. 团队主线与个人模式使用同一知识对象和运行合同。`workspace_mode=team|personal` 只能改变默认信息
    架构、术语和推荐任务，不能创建第二套 Agent Loop、检索器、记忆仓或学习控制面。
26. 团队内部知识优先级高于公共论文，但不能覆盖来源事实。回答必须显示来源层级；发生冲突时按
    有效期、版本、信任和显式 supersedes 关系解释，不能仅按向量分数选择结论。
27. 前端必须先呈现用户任务和答案，再按需展开检索、图谱和学习细节。不得要求普通研发用户先理解
    Chunk、RRF、candidate、ChangeSet 或 Canary 才能使用系统。
28. 连接器在体验闭环完成前属于延期项。当前只要求手工上传、目录样例导入和可重复 fixture；不得
    用大量半成品连接器稀释聊天、知识、引用、图谱和反馈的完成度。

## 产品优先级

1. 研发用户首次使用、核心对话和证据查看体验。
2. 证据正确性、来源层级与可追溯性。
3. 团队知识、个人知识和公共参考知识的清晰边界。
4. 多模态知识摄取、Agentic 检索与任务完成率。
5. 自进化的可理解、可评测、可撤销性。
6. 个人数据安全、团队 scope 与可删除性。
7. 可测试性、长期扩展性和开发便利性。

## 当前非目标

- 自动训练或更新基础模型权重。
- 无审计的原生学习、无评测门禁的高影响在线自修改，或把一次回答直接保存为永久真相。
- 任意 Shell、SQL、Cypher、文件删除或网络写操作。
- 一开始实现大量自治 Agent、无限递归委派或无预算后台任务。
- 在没有视觉纵向验收前宣称已经完成多模态。
- 第一阶段自动处理所有音频、视频、邮件、日历和桌面控制；这些按明确用例逐步接入。
- 在核心代码中写死科研、医疗、金融等领域 ontology。
- 当前阶段批量实现 GitHub、飞书、Jira、Confluence、Slack 或企业微信连接器。
- 把 528 篇 arXiv 论文默认混入企业演示工作区，或用论文数量代替企业问答体验验收。
- 为个人版和团队版维护两套聊天、检索、图谱、记忆或学习实现。

## 完成定义

项目不是以“能聊天”为完成，而是以下闭环同时成立：

1. 用户能导入至少文本/PDF/图片，原件与所有派生产物可追溯。
2. Agent 能通过 Tool Calling 对复杂问题执行多轮混合检索、图谱检索和受控公共网络检索，
   并以流式方式返回结果。
3. 文本结论和视觉结论都能定位到文档片段、页码、图片或视觉区域；证据不足时稳定降级。
4. 一次任务可被追踪和回放，个人偏好与长期记忆可查看、修改、撤销和删除。
5. 重复成功经验可形成候选 Memory 或 Skill；候选经过离线评测进入 shadow/canary，失败会
   生成回归用例，所有晋级可回滚。
6. 跨任务经验能按 Semantic Memory、Procedural Skill、Harness Policy 三条路径固定化；每条路径
   都有唯一所有者、来源、精确版本、评测、作用域和回滚语义，当前 run 不会因后台学习而漂移。
7. Postgres、Qdrant、Neo4j 和对象存储职责清晰，重启、并发、失败重试与数据隔离通过真实验收。
8. 新用户能在 3 分钟内导入示例研发知识、提出第一个问题、理解答案来源并继续追问；简单社交消息
   不误触发 RAG，专业问题能显示检索进度、引用和必要的图谱路径。
9. 团队研发 fixture 的直接事实、多文档综合、图谱多跳、时效冲突、事故复盘、提示注入和无答案
   用例全部达到规定门槛；桌面与 390 x 844 移动端无重叠、横向溢出或不可达操作。

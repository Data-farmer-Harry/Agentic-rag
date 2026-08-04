# HermesGraph Engineering Intelligence Agent

产品需求文档（PRD）

版本：v1.0

日期：2026-08-03

状态：研发团队主线与体验优先级已锁定，持续实现

## 1. 文档说明

本产品是一套 Hermes-first、OpenAI-powered 的自进化多模态 Agentic RAG 平台。商业主线是服务
AI/软件研发团队的 Engineering Intelligence Agent：理解内部架构、服务、API、技术决策、事故、
Runbook 和研发资料，完成有来源的问答、影响分析、入职学习和技术调研。系统同时保留通用个人能力，
可处理论文、笔记、任务、长期记忆与个人学习；两种模式共享同一 Agent、检索、图谱、记忆和学习
控制面。Hermes Agent 负责唯一的在线 Agent Loop、会话、原生 Memory/Skill、Todo 和后台回顾；
OpenAI Python SDK 提供 Responses API、Tool Calling、Structured Outputs、Vision、Embeddings 和
需要时的 Background Tasks；HermesGraph 负责 Agentic RAG、知识图谱、作用域隔离、证据发布和
学习治理。

产品的长期差异化能力是 Hermes 原生自学习与 HermesGraph 治理学习的组合：Hermes 可以在持久 profile 中持续维护个人 Memory/Skill；每次原生写入都镜像为待审计 ChangeSet。HermesGraph 同时记录任务轨迹、证据、视觉理解结果和用户反馈，从成功经验中提炼受治理技能，从失败中生成回归用例，并在通过评测门禁后改善检索策略、提示词、实体别名和高影响流程。这里的“自进化”默认是非参数化学习，不自动改模型权重、不自动修改核心业务代码。

## 2. 产品定位

### 2.1 一句话定位

一个能理解研发团队与个人资料、主动连接架构和经验、使用工具完成知识任务，并在受控评测下逐渐
学会团队与个人工作方式的 Engineering Intelligence Agent。

### 2.2 目标用户

| 用户 | 主要任务 | 关键痛点 |
| --- | --- | --- |
| AI/软件研发工程师 | 查询服务、API、架构、历史决策和故障经验 | 文档分散、上下文切换、内部术语和依赖难追踪 |
| 新加入项目的工程师 | 建立系统地图、找到负责人、完成首周学习 | 不知道从哪里开始，资料版本和可信度不清楚 |
| 技术负责人/架构师 | 影响分析、方案比较、技术调研和知识治理 | 关键决策埋在文档与聊天中，组织经验难复用 |
| 重度知识工作者 | 搜索笔记、文档、截图和项目资料 | 个人信息分散，关键词和文件夹无法表达关系 |
| 研究人员/工程师 | 比较方法、追踪决策、理解图表与代码资料 | 文本、PDF、图片和历史上下文彼此割裂 |
| 创作者/独立开发者 | 整理灵感、回顾素材、复用工作流 | Agent 每次从零开始，无法积累可控能力 |
| 长期个人用户 | 问答、总结、计划、回顾和自动化重复任务 | 通用助手不理解私人知识，也难以纠错和遗忘 |

### 2.3 领域策略

产品支持 `team` 与 `personal` 工作区体验，但底层始终强制 tenant/project/user 作用域。主线
DomainPack 是软件研发，核心实体包括 Service、Repository、Module、API、Team、Owner、Decision、
Incident、Runbook、Requirement 和 Technology；个人通用知识与计算机学习是兼容模式。领域实体类型、
关系、检索模板、输出 schema 和评测集仍通过 `DomainPack` 扩展，不能把研发本体写死进核心模型。
医疗诊断、法律结论、投资决策等高风险领域不作为第一版本的自动决策场景。

### 2.4 当前交付策略

- 体验优先于连接器数量：先完成手工上传、示例知识库导入、对话、引用、图谱路径、反馈和恢复。
- GitHub、飞书、Jira、Confluence、Slack 和企业微信连接器延期，不作为当前完成门槛。
- 528 篇 arXiv 论文暂时退出默认团队工作区，后续作为个人/公共补充知识按需启用。
- 第一套业务纵向验收使用虚构企业研发知识库，禁止依赖真实公司数据或用户隐私。

### 2.5 核心能力与产品职责

| 核心能力 | 产品职责 |
| --- | --- |
| Responses API | 文本/视觉理解、复杂响应、结构化抽取、hosted Web Search 和需要时的长任务模型调用 |
| Tool Calling | 搜索知识、查图谱、读取记忆、激活 Skill 和受控外部能力 |
| Structured Outputs | 计划、实体关系、证据判断、回答合同和学习候选 |
| Vision | 图片、截图、扫描件、图表和 PDF 页面视觉语义 |
| Embeddings / Vector Search | 文本与图像派生语义的向量召回、过滤和相似性连接 |
| Background Tasks | 长时摄取、重索引、评测和用户明确开启的后台任务 |
| Hermes Agent 0.19.0 | 唯一在线 Agent Loop、会话、Todo、原生 Memory/Skill 和后台回顾 |
| LangChain | loader、splitter、retriever、LCEL 数据流、结构化转换、adapter 和 callbacks |

## 3. 用户问题与机会

传统关键词搜索只能找到相似文本，普通 RAG 通常也是“检索若干片段后直接回答”。个人知识问题经常需要：

1. 先判断问题是事实查找、方法比较、趋势分析还是证据冲突。
2. 同时查询正文、笔记、图片/截图视觉内容、时间与来源元数据，以及实体关系图谱。
3. 沿着“项目 -> 决策 -> 人物 -> 文档”“概念 -> 图片 -> 事件”或专业 DomainPack 路径进行多跳检索。
4. 把每一个关键结论绑定到原始片段、页码、图片、视觉区域、表格或图号。
5. 发现资料不足、来源冲突或证据不能推出结论时，主动降低结论强度。
6. 让系统记住用户偏好、历史判断和已验证的工作流程，而不是每次从零开始。

## 4. 产品目标与非目标

### 4.1 目标

- 提供可追溯的研究问答，每个关键结论都能展开查看证据。
- 提供文本、PDF 与图片的一体化个人知识收件箱，保留原件和视觉派生产物。
- 将向量检索、关键词检索、知识图谱检索和重排统一成 Agent 可调用的检索工具。
- 知识图谱工具必须覆盖实体名称/别名解析、多跳证据子图、实体连接与共同邻居对比、冲突关系检索；
  scope 与 Cypher 由服务端控制，每条可用关系必须回连原始 Chunk 证据。
- 将当前公共事实的联网检索纳入同一证据合同，同时与个人私有知识保持不同 scope 和 trust。
- 支持复杂问题的分解、并行检索、补充检索、证据校验和答案修订。
- 支持文档、图片、截图、网页和结构化资料的增量导入、去重、版本化和失败重试。
- 支持用户反馈、任务轨迹评估、技能提炼、回归测试和技能版本晋级。
- 对高风险操作、外部写入、知识库变更和自学习资产升级提供审核与回滚。

### 4.2 非目标

- 第一版本不做模型权重训练和在线强化学习。
- 第一版本不让 Agent 直接执行任意 Cypher、SQL、Shell 或删除操作。
- 第一版本不承诺完全自动生成严谨的学术结论；系统必须允许回答“证据不足”。
- 第一版本不替代专业人员进行医疗、法律、财务和安全决策。
- 第一版本只运行 Hermes Agent 一个在线循环；不叠加 LangChain Agent、LangGraph 或 OpenAI Agents SDK Runner。
- 第一版本先完成图片/Vision 纵向闭环；音频、视频、邮件、日历和桌面控制不冒充已完成能力。

## 5. 核心用户故事

### 5.1 个人知识问答

作为长期用户，我问“上个月我为什么把检索架构从单向量改成混合检索”，系统应联合搜索设计文档、会议截图、任务轨迹和知识图谱，区分原始记录与系统推断，并给出可展开的文本或图片证据。

### 5.2 多模态理解

作为用户，我上传一张架构图、扫描页或产品截图。系统应保留原图，使用 Vision 生成严格结构化的描述、可见文字、区域和实体关系候选；之后我能按画面语义检索它，并定位回原图或区域。

### 5.3 研究问答

作为研究人员，我问“GraphRAG 相比普通向量 RAG 在多跳问题上解决了什么问题”，系统应先识别比较任务，检索代表性论文和实验结果，沿图谱找到任务、数据集和指标，输出差异、边界、证据和未解决问题。

### 5.4 方法比较

作为算法工程师，我问“A 和 B 方法如何选择”，系统应生成统一比较维度：目标、输入、核心机制、训练成本、推理成本、适用数据、优势、限制、复现难度，并标明哪些是来源直接陈述、哪些是基于来源的推断。

### 5.5 综述与证据矩阵

作为研究助理，我提供一个主题和时间范围，系统应生成候选文献集、去重后的方法谱系、论文-方法-数据集-指标矩阵、冲突证据列表，并允许导出 Markdown、CSV 和 BibTeX。

### 5.6 资料导入

作为个人用户，我上传一批 PDF、图片或提供网页 URL，系统应异步解析、切分、抽取实体关系、生成向量、建立索引，并显示每个对象的处理状态和失败原因。

### 5.7 Hermes 原生学习与治理学习

作为长期用户，我连续几次要求“比较两种研究方法”。Hermes 可把稳定偏好和低风险工作方式写入原生 Memory/Skill，并在学习日志中留下待审计变更；影响检索策略、证据规则或共享流程的技能必须形成 HermesGraph 草稿，经过离线回放和回归测试后才以 shadow/canary 参与后续任务，效果下降时可回滚。

### 5.8 当前公共事实

作为用户，我询问近期变化、最新公开规范或个人知识库之外的事实。系统应只在需要时调用
受控 Web Search，优先官方/一手来源，把 URL citation 转换为本轮证据；查询中出现疑似凭据或
私密记录时不得发网，无引用结果不得支撑结论，网络来源不能仅凭“有链接”升级为 verified。

### 5.9 个人连续性与行动

作为长期用户，我可以让 Agent 创建和维护任务、计划、步骤、检查项和笔记；首次使用时设置称呼、
沟通偏好、兴趣与边界；每天生成可编辑的第一人称归档并从月历回看；也可以用自然语言要求忘记或
更正记忆。当前表达状态可以自动归约或临时覆盖，但不能改变事实、证据、权限和安全判断。

## 6. 核心功能范围

### 6.1 工作台

- 对话区：流式展示思考阶段的公开状态、工具进度、最终回答和引用。
- 证据侧栏：按结论分组展示来源、文档、页码、原文片段、检索路径和置信度。
- 图谱视图：展示问题相关的实体、关系、路径、来源和时间范围。
- 任务视图：显示当前任务的计划、子任务、工具调用、失败重试和人工审批。
- 反馈操作：赞/踩、标记错误来源、标记缺少证据、保存为技能候选、加入回归集。

### 6.2 知识库管理

- 数据源注册：文件夹、上传、图片/截图、URL、Sitemap、Git 仓库、对象存储。
- 文档生命周期：发现、下载、解析、切分、抽取、索引、发布、归档、删除。
- 多模态对象：原始媒体、MIME、尺寸/页码、视觉描述、OCR 文本、区域、派生版本和模型 revision。
- 文档版本：保存 content hash、来源 URL、抓取时间、解析器版本和 embedding 版本。
- 质量检查：空文本、重复文档、页码丢失、表格解析失败、实体置信度过低、引用断裂。
- 增量更新：仅对变化文档重新处理，保留旧版本用于审计和回放。
- arXiv 语料同步：按 `cs.AI/cs.CL/cs.IR/cs.LG/cs.CV/cs.SE/cs.HC` 和主题查询拉取元数据，
  以 arXiv ID/version、更新时间和内容 hash 幂等同步；默认先筛元数据，再在磁盘、数量、时间
  和并发预算内下载 PDF，支持断点续跑和失败重试。
- 公共与个人知识分层：arXiv 论文标记为公共参考来源，个人文档/图片保持私有来源；两者可在
  同一问题中联合检索，但证据 UI、信任等级、删除策略和导出权限必须区分。

### 6.3 检索与推理

- 查询意图分类：lookup、compare、timeline、landscape、synthesis、debug、unknown。
- 查询改写：实体规范化、同义词扩展、时间范围解析、结构化过滤条件生成。
- 混合检索：Qdrant dense + sparse/BM25 + metadata filter + reranking。
- 图谱检索：实体邻居、关系路径、时间过滤、子图扩展、共同邻居、方法谱系。
- 公共网络检索：仅用于当前/变化/外部公开事实；支持独立 scope、调用预算、domain allowlist、
  URL 安全校验、citation 归一化和 provider 失败降级。
- 多跳策略：最多 N 轮规划，支持并行检索、基于证据缺口的补充检索。
- 证据校验：来源存在性、结论-证据覆盖、时间一致性、冲突检测、引用格式检查。
- 置信度分级：verified、supported、inferred、insufficient、conflicting。
- 跨模态检索：文本查询可召回图片/截图，视觉证据可参与图谱路径与回答引用。

### 6.4 通用 Agent 行动与个人控制面

- 使用 Hermes Agent sidecar 维护单一在线 Tool Calling 循环；OpenAI SDK 负责底层模型能力。
- Hermes 通过受认证、run-scoped 的 Capability Bridge 使用知识检索、图谱、Web、项目记忆和答案发布，不能直接访问数据库 driver。
- 复杂查询先产生受约束计划，按证据缺口决定是否继续检索，而不是固定调用所有工具。
- 长时摄取、重索引、评测和用户明确委派的任务可进入 durable background job。
- 任何写入个人数据、发布 Skill 或调用外部副作用工具都经过权限、确认和审计。
- 提供 Task/Plan/PlanStep/Checklist/Note 的结构化生命周期、项目/用户作用域、乐观版本和
  append-only event；Hermes 使用专用 personal tools，不能把临时 native Todo 隐式双写为永久任务。
- Persona/Onboarding 保存可编辑的沟通偏好、兴趣和边界，并在 run start 以 bounded capsule 冻结。
- Day Archive 按本地时区汇总对话、完成任务和开放事项，生成 summary、第一人称 diary 和月历。
- 自然语言 Memory correction 支持 forget/replace；多候选必须由用户确认，旧记录只 revoke 不篡改。
- Emotion 使用确定性 reducer 与 TTL override，只影响表达方式。

### 6.5 自学习与技能

- Episodic memory：保存任务轨迹、工具调用、证据、用户反馈和最终结果。
- Semantic memory：保存可验证事实、用户偏好、实体别名和知识库变更。
- Procedural memory：保存可复用技能、触发条件、步骤、工具约束和成功指标。
- Hermes native learning：原生 Memory/Skill 在持久 profile 中生效，并同步 `native_applied/requires_audit` ChangeSet；支持查看、撤销和回滚。
- Reflection：任务后分析失败类型、检索缺口、错误引用和无效工具调用。
- Structured Reflection：反馈、失败或审计信号触发 Responses API 严格输出；模型只生成候选，确定性评估、作用域绑定和 Memory Write Gate 决定是否持久化。
- Skill mining：从多次成功轨迹提炼技能草稿，而不是从单次偶然结果生成技能。
- Skill refinement：使用新的反馈和回归样例更新技能版本。
- Counterfactual replay：在只读、有界、冻结能力输出的沙箱中实际执行候选步骤；顺序偏差、工具失败、预算越界或未消费 fixture 均不能通过。
- SemVer lineage：父版本不可变；证据扩展、兼容行为变化和 breaking change 分别生成 patch、minor、major Draft，并重新经过完整晋级门禁。
- Promotion gate：draft -> security review -> offline pass -> shadow -> canary -> active -> deprecated/rolled back；进入 Canary/Active 必须人工批准。
- Forgetting：技能过期、知识源失效、连续失败或用户撤销后自动降级。
- Health rollback：Canary/Active 的实际激活样本达到最小窗口后，质量、失败率或 unsupported claim 指标越界会自动回滚并生成审计 ChangeSet。

### 6.6 管理与治理

- 租户、项目、知识库、角色和权限。
- 工具权限、审批策略、速率限制、预算限制和模型路由。
- 提示词、检索策略、技能、评测集和 schema 的版本管理。
- 数据脱敏、敏感内容不入 trace、审计日志和删除请求。
- 运行看板：延迟、成本、召回率、引用覆盖率、工具错误率、技能收益。

## 7. 产品交互原则

1. 先给答案，再给证据；证据必须可展开。
2. 事实、来源直接陈述和 Agent 推断要用不同标签区分。
3. 不能验证的内容要明确说出缺口，不能用流畅表达掩盖缺证据。
4. 长任务要显示阶段状态，不能让用户面对无响应的空白等待。
5. Agent 的自我改进必须可见、可比较、可回滚。
6. 默认最小权限，所有写入类工具都必须有策略检查。

## 8. 功能优先级

| 优先级 | 功能 | 验收标准 |
| --- | --- | --- |
| P0 | 文本/PDF/图片导入与状态 | 原件和派生产物可追溯，失败可重试且有原因 |
| P0 | Vision 理解 | 图片/截图可提取描述、可见文字、区域和实体关系候选，并定位回原图 |
| P0 | 混合检索 | 对固定评测集输出 Recall@20、MRR、nDCG |
| P0 | 跨模态检索 | 文本问题能召回对应图片证据，跨作用域结果为零 |
| P0 | arXiv 计算机语料同步 | 查询、元数据、PDF 下载、hash 去重、断点恢复和来源许可字段可审计 |
| P0 | 图谱实体关系 | 关键实体可追溯到原文片段和文档版本 |
| P0 | Agent 工具循环 | 支持计划、检索、证据验证、回答和最大轮次保护 |
| P0 | 受控公共 Web Search | 当前事实可返回本轮 URL citation；敏感查询、私网 URL、无引用输出和越过 domain policy 的来源被阻断 |
| P0 | Web Search 固定门禁 | 版本化集合覆盖 live 质量与离线安全/故障合同；fixture 不计入 provider-only success，报告不保存原始查询或网页正文 |
| P0 | 引用回答 | 关键结论引用覆盖率达到目标阈值 |
| P0 | 轨迹与评估 | 每次运行可查看 trace、工具参数、结果和评估分数 |
| P1 | 多跳比较/综述 | Agent 根据证据缺口迭代检索并生成结构化证据矩阵 |
| P1 | 技能草稿 | 从重复任务中生成技能候选，状态可审计 |
| P1 | 回归门禁 | 技能晋级必须通过离线测试和安全检查 |
| P1 | 反事实 Skill 回放 | 声明式步骤在冻结能力沙箱执行；报告只保存 hash/错误码/指标，不保存敏感工具原文 |
| P1 | 跨版本 Skill refinement | 同一 Skill ID 可并存多个 SemVer 版本，父版本不可变，评估、迁移和对账可指定精确版本 |
| P1 | 渐进式技能发布 | Shadow 样本达标后才可人工批准 Canary；Canary 实际激活样本达标后才可批准 Active，退化自动回滚 |
| P1 | Durable 学习恢复 | Reflection 先 checkpoint；artifact 写入受 lease fencing 保护；Skill 允许/拒绝/回滚有 append-only ledger；多 worker 重试不覆盖不可变记录 |
| P1 | 用户画像 | 记住用户偏好，但能查看、修改和删除 |
| P1 | Personal Control Plane | Task/Plan/Note、Persona、Day Archive、Memory correction 和 Emotion 通过 scoped API、Hermes tools 与 UI 闭环 |
| P2 | 音频/视频知识 | 转写、关键帧、时间码引用和跨模态检索通过独立评测 |
| P2 | 个人后台任务 | 按用户明确配置执行简报、重索引或长期研究任务 |
| P2 | 多渠道接入 | API、Web、邮件/日历或个人工具连接器 |
| P2 | 多租户高可用 | 横向扩展、任务恢复和跨区域部署 |

## 9. 关键指标

### 9.1 产品指标

- 首次有效回答率：用户无需重问即可接受的回答比例。
- 证据点击率：用户打开来源证据的回答比例。
- 任务完成率：任务达到目标状态且未触发人工纠正的比例。
- 技能复用率：命中已发布技能的相似任务比例。
- 技能收益：启用技能后，相对于基线的任务成功率、成本和延迟变化。

### 9.2 技术指标

- Retrieval Recall@20、MRR、nDCG@10、实体链接准确率、关系抽取 F1。
- Citation coverage、citation precision、unsupported claim rate。
- p50/p95 首 token 延迟和端到端延迟。
- 每任务 token、模型成本、向量检索耗时、图查询耗时。
- 工具错误率、超时率、重试率、最大轮次触发率。
- 学习资产回滚率、技能漂移率、恶意记忆拦截率。

### 9.3 初始目标

初始目标只作为工程预算，必须用真实语料和评测集重新校准：

- 简单问答 p95 端到端延迟小于 12 秒。
- 复杂多跳任务 p95 小于 60 秒，支持异步继续执行。
- 关键结论引用覆盖率不低于 90%。
- 高置信答案的 unsupported claim rate 小于 5%。
- 关键检索集 Recall@20 不低于 85%。
- 技能晋级前后，核心评测集不得出现超过 2% 的回归。

## 10. 版本路线图

### Phase 0：基础设施与文本基线

建立 canonical contracts、Capability Registry、DomainPack ABI、RunSnapshot、API、基本 trace 和两个小型领域包。先用本地存储形成可回放闭环，再接入 Postgres、Qdrant 和 Neo4j。DomainPack 不是后期优化项，而是从第一天验证核心领域无关性的边界。

### Phase 1：在线 Engineering Intelligence Agent 与 Agentic GraphRAG

以 Hermes Agent sidecar 提供单一 Tool Calling Loop，通过受信任插件连接 HermesGraph 的混合检索、图谱检索、项目记忆、来源获取和证据发布。Responses API 等 OpenAI 能力由后端组件直接调用。支持 lookup、compare 与 personal-recall，并使用固定评测证明不是一次性 RAG。

### Phase 2：Vision 多模态团队与个人知识

把图片、截图、扫描件和 PDF 页面视觉内容升级为一等 Knowledge Object；加入 Vision 结构化抽取、OCR/区域、跨模态检索、图片证据引用和视觉回归集。

### Phase 3：Hermes 原生学习与治理闭环

启用 Hermes 原生 episodic/semantic/procedural learning、Skill 管理和后台回顾，并把原生写入镜像到审计仓。高影响学习继续使用 reflection、技能生成、离线回放、shadow/canary 晋级和回滚；Memory/Skill/Evaluation/Observation/ChangeSet/Transition 使用 Postgres durable control plane，旧 worker 失去 lease 后不能继续写入受治理资产。

### Phase 4：复杂工作流与更多模态

加入综述、时间线、冲突证据、durable background task、人工审批和导出；按明确需求接入音频转写、视频关键帧、邮件/日历或个人连接器。

### Phase 5：生产化与领域迁移

加入多租户、数据治理、权限隔离、可靠任务恢复、成本优化和更多领域包；DomainPack 插件机制本身已在 Phase 0 建立。

### 10.1 当前交付边界

| 阶段 | 当前状态 | 尚未完成 |
| --- | --- | --- |
| Phase 0 | 已交付基线 | 扩大 reference DomainPack 的开放分布验证 |
| Phase 1 | Hermes 0.19.0 sidecar、插件 bridge、严格证据发布、conversation history、正常 finalizer 和每回合 Memory/Skill review 已实现；OpenAI Agents SDK fallback 已删除；Web Search 6/6 离线合同通过 | 最终 review completion live 重验受 provider `429 model_cooldown` 阻断；7-case Web Search live 门禁和生产 embedding live 校准延期 |
| Phase 2 | 已交付图片/Vision 首条纵向闭环 | 图片原生 embedding、PDF 自动选页、音频和视频 |
| Phase 3 | 已交付 Hermes 原生 Memory/Skill 写前快照、脱敏审计、接受/条件回滚 API、durable 自进化主链路、冻结能力 replay 与 SemVer refinement | 原生学习审核 UI、快照保留/GC、扩大开放分布 replay、真实 provider/tool 仿真与可视化版本比较 |
| Phase 4 | 部分基础能力已存在 | 综述导出、长时审批工作流、邮件/日历等连接器仍属规划 |
| Phase 5 | 未交付 | 多租户高可用、RBAC、备份恢复、生产 SLO 与跨区域部署 |

Phase 3 当前已包含 Postgres learning job、reflection checkpoint、Memory/Skill/Evaluation/
Observation/ChangeSet/Transition 审计资产、Shadow/Canary 健康门禁、人工晋级、自动回滚，
以及 migration v9 的同事务确定性 stage commit、migration v10 的精确 Skill version link/result/
checkpoint/reconciliation。离线门禁实际通过 `SkillExecutionRegistry` 在冻结能力 fixture 上执行候选，
并保存逐步 hash、失败码和聚合指标；已观测父版本可派生 SemVer Draft。外部模型调用到 reflection
checkpoint 之间仍是 at-least-once 边界，不承诺跨 provider 与 Postgres exactly-once。

Hermes 原生 Memory/Skill 是独立的先应用后审计通道：插件在执行前把目标文件/Skill 目录保存到
`hermes_data` 快照区，成功写入后记录来源 run、scope、脱敏参数、before/after hash、风险与回滚
条件。审核者可接受变更，或在当前 hash 仍等于 audited after hash 时确定性恢复 before snapshot；
后续学习已改变目标时必须拒绝回滚。它不自动获得 HermesGraph
`offline_pass/shadow/canary/active` 状态，也无权修改图谱 active 事实、检索安全策略或外部写权限。

## 11. 风险与产品决策

| 风险 | 影响 | 决策 |
| --- | --- | --- |
| 自动生成错误技能 | 持续放大错误 | 必须经过离线回放，默认 shadow，不直接 active |
| Hermes 原生学习质量不稳定 | 错误偏好或流程持续影响后续会话 | 原生写入镜像待审计 ChangeSet，提供撤销/回滚；高影响资产不走原生直写 |
| 记忆被提示词注入污染 | 后续任务持续错误 | 记忆分级、来源绑定、写入扫描、可撤回 |
| 图谱抽取错误 | 多跳答案被错误关系放大 | 每个节点/边保存证据和置信度，答案阶段再验证 |
| Agent 无限循环 | 成本和延迟失控 | max_turns、工具预算、超时、重复调用检测 |
| 来源失效或版本变化 | 引用不可复现 | 保存文档快照、content hash 和解析版本 |
| 用户把推断当事实 | 研究决策风险 | 输出标签、证据强度和显式不确定性 |
| 框架升级破坏行为 | 需要重构 | 领域接口与框架适配器隔离，锁定依赖并维护 contract tests |

## 12. 验收标准

当以下条件全部满足时，Phase 1 MVP 可交付：

1. 个人默认 DomainPack 和至少一个 reference DomainPack 能在不修改核心代码的前提下完成导入、检索、证据回答和评测。
2. 同一问题能通过 Capability Contract 联合调用向量检索、图谱检索和来源获取能力。
3. 对 compare 问题输出结构化比较表，每一项有来源或明确标记为推断。
4. 图谱节点和边都能回溯到原始文档、页码/段落和内容 hash。
5. 所有运行都能关联 trace、任务、工具调用、证据和最终答案。
6. 触发超时、工具失败、无证据和冲突证据时，系统能给出稳定的降级行为。
7. 通过固定评测集的质量、延迟、安全和成本门禁。
8. 需要当前公共事实时可通过 `web:read` 能力搜索并返回运行级 URL provenance；只有
   untrusted Web citation 时，最终置信度最高为 supported。
9. Web Search 固定门禁必须分别报告 contract 与 live 结果；密钥、私网、提示注入、无引用、
   timeout/5xx required case 不得被平均分掩盖，live 必须报告 provider-only success、引用覆盖、
   来源精确率、P50/P95 和 token usage。
10. Hermes 在线 run 必须只能通过 bridge 暴露的能力工作；完成前必须调用
    `hermesgraph_publish_answer`，未知 evidence ID、发布后继续调工具或未经批准的副作用请求均失败关闭。
11. Hermes 原生 Memory/Skill 成功写入必须保留来源 run、scope、写前快照与 before/after hash，并生成
    `requires_audit` ChangeSet；正文不复制到审计日志，接受/成功回滚/失败回滚采用 append-only review
    记录，不能被报告成已经通过 HermesGraph promotion gate。

Phase 2 多模态可交付还必须满足：图片原件可回看；Vision 输出通过严格 schema；文本问题可召回图片；视觉结论能定位到图片/区域；归档和跨 scope 均立即失效；模型失败不会留下 active 半成品。

Phase 3 自进化可交付还必须满足：重复成功轨迹只生成 Draft；候选在冻结能力沙箱中执行并通过安全/非退化门禁；父版本保持可查询；新版本具有 `parent_version`、SemVer change level 和 semantic diff；所有评估、迁移、checkpoint、result 与 reconciliation 均绑定精确版本；Canary/Active 仍需要健康样本和人工批准。

## 13. 参考资料

- [Hermes Agent 官方文档](https://hermes-agent.nousresearch.com/docs/)
- [Hermes Agent 官方仓库](https://github.com/NousResearch/hermes-agent)
- [OpenAI Responses API 官方文档](https://developers.openai.com/api/docs/guides/responses)
- [Neo4j GraphRAG for Python](https://neo4j.com/docs/neo4j-graphrag-python/current/)
- [Qdrant Hybrid Search](https://qdrant.tech/documentation/search/text-search/hybrid-search/)

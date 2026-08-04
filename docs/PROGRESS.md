# HermesGraph Progress

最后更新：2026-08-03

## 当前阶段

产品北极星已锁定为 **Hermes-first、OpenAI-powered、自进化、多模态的 Engineering Intelligence Agent**。研发团队的内部知识问答、系统理解、事故复盘、影响分析和工程入职是默认业务主线；个人论文、笔记、任务和长期学习继续由同一套内核支持，不拆第二套 Agent。Hermes Agent `0.19.0` 是唯一在线 Agent Runtime；HermesGraph 继续拥有 Agentic RAG、Qdrant/Neo4j 证据层、Postgres durable learning、Vision/Structured Outputs 和严格答案发布。MemoHarness Experience Bank、D1-D6、Pattern evaluator、Promotion Evidence、transition ledger、稳定 Canary 分桶与 bounded consumer 已落地；生产数据仍保守为 0 Draft Pattern，MH-015 health/auto rollback 尚未完成。OpenAI Agents SDK fallback 已删除；OpenAI Python SDK 继续提供 Responses、Structured Outputs、Vision、Embeddings、Web Search、图谱抽取和反思。

当前产品优先级已经切换为“可直接使用”：独立会话、新建/切换/刷新恢复、按 session 草稿、显式
长期记忆、会话重命名/归档/恢复、聊天附件、带说明反馈和失败重试、可跳过的 Persona 首次设置、
历史会话搜索、真实运行耗时反馈、聊天快速记录任务/日程/笔记、按日回顾和本地到期提醒已完成。
提醒从现有 Task `due_at` 确定性投影，支持未读、延后、任务跳转和显式授权后的浏览器桌面通知。
复杂 Pattern 自动回滚保留设计但延期，不再占用近期交互主线。

## 进度

| 工作项 | 状态 | 产物 | 验证 |
| --- | --- | --- | --- |
| PRD 与技术设计基线 | 完成 | `docs/PRD.md`、`docs/TECHNICAL_DESIGN.md` | 人工审阅 |
| 研发智能 Agent 完整交付设计 | 完成（设计基线） | `docs/ENGINEERING_INTELLIGENCE_AGENT_DELIVERY.md` | 体验 P0、七阶段门禁和 Definition of Done 已冻结 |
| 模拟企业研发知识库 | 完成（fixture v1） | `examples/enterprise_knowledge/`：23 份语料、manifest、10-case 黄金题集 | JSON/路径/ID/时效冲突/安全样本静态校验 |
| 意图锁定 | 完成 | `docs/INTENT.md` | 架构不变量已记录 |
| 个人多模态需求重对齐 | 完成（文档基线） | Intent v2026-07-15、PRD v0.3、TECH v0.5 | 明确计算机优先、OpenAI 原语、Vision、arXiv 与自进化验收 |
| LangChain 职责修正 | 完成 | 技术设计 0、1、8、18、21 节 | 禁止双 Agent Loop |
| 工程骨架 | 完成 | `app/`、`tests/`、`pyproject.toml`、两个 dependency lock | 332 collected：315 passed、17 个环境型 skip；Ruff 全绿，163 个应用源码 strict mypy 通过 |
| Hermes Agent 主运行时 | 完成（0.19 contract/sidecar/live 主回合）/待补最终握手 live 重验 | `app/agent/hermes_runtime.py`、`app/agent/hermes_bridge.py`、`deploy/hermes/` | Hermes 0.19.0 healthy；真实 Agent 首发约 12 秒返回、主 run 正常 completed、后台 review 实际调用 memory/skills；completion 握手 contract 通过，最后 live 被 provider 429 阻断 |
| OpenAI 模型能力层 | 完成（SDK 原语） | 官方 `openai` Python SDK、Responses/Structured Outputs/Vision/Embeddings/Web Search adapters | 不拥有 Agent Loop；`openai-agents`、`RUNTIME_MODE=openai` 和 SDK session 已删除 |
| OpenAI-compatible provider | 可用（批量长尾仍需重试） | 共享 model client、宿主/Docker base URL | v6 的 5-case/18-case Structured Outputs 门禁通过；固定 12 并发两批分别 13/20、15/20，失败均为 timeout |
| LangChain Integration Runtime | 完成（P0） | LCEL pipeline、Capability Registry、callbacks、adapters | scope/schema/timeout/partial failure 测试通过 |
| 受控公共 Web Search | 完成（adapter + v1 contract）/延期（当前 live） | Responses hosted adapter、`web:read`、13-case golden set、评测 CLI、原子脱敏报告 | 6/6 contract、Web 定向 12/12、当前全套 294 passed；7-case provider gate 待可用凭据恢复后执行 |
| 本地知识图谱 | 完成（P0） | typed graph、evidence relationship、allowlisted traversal | 邻居、路径、冲突和租户隔离测试通过 |
| GraphRAG Tool Suite | 完成（后端 v1） | 实体解析、证据子图、实体对比、固定模板遍历、Hermes/API/Capability 合同 | 本地拓扑、Neo4j 参数化/scope/evidence contract、Hermes schema/budget/citation、真实 Compose 三接口通过 |
| Computer Workspace Toolset | 完成（安全只读 v1） | list/read/search、文本/代码/PDF/DOCX/XLSX、root/scope/symlink/secret/预算门禁 | capability/bridge/plugin/publisher/Office extraction 单元测试与真实 Compose capability 注册通过 |
| Hermes Memory 与 Skill | 完成（P0） | memory gate、capsule、progressive skill registry | 写入、撤回、隔离、安全解析测试通过 |
| Personal Control Plane | 完成（P1 v2） | Task/Plan/Step/Checklist/Note、Persona/Onboarding、Day Archive/Diary/Calendar、Memory correction、Emotion、Task reminder projection | JSON/Postgres v11/v15、6 个 Hermes tools、提醒 API/工作台、真实 Compose/浏览器纵向通过 |
| Hermes 原生学习治理 | 完成（后端 v2） | 写前 file/tree 快照、before/after hash、append-only review、接受/条件回滚、retention/GC、容量/备份健康、内网 admin | 未审阅 fail-safe 保留、accepted/rolled-back/no-change 期限、1 GB 容量门禁、鉴权与 API scope 测试通过 |
| 学习闭环 | 完成（P1 durable v10 + quantified promotion） | Structured reflection、稳定 miner、冻结能力 replay、SemVer refinement、不可变 promotion evidence、按 run 反馈覆盖、Shadow/Canary health、人工晋级、建议/自动 rollback、Postgres durable ledger | 轻度负反馈建议回滚、严重负反馈立即回滚、窗口退化回滚、证据 scope/version 校验和幂等审计通过；全套回归通过 |
| 会话感知路由 | 完成（v2） | scope 隔离历史、独立 `gpt-5.6-luna` 快速通道、typed routing lane、22-case 黄金集与评测 CLI | 无历史确认确定性返回；有历史确认进入模型；事实/知识/行动 fail-safe Hermes；静态合同全绿，live gate 见 2026-07-30 记录 |
| MemoHarness 固定化记忆 | Phase 1-4、MH-014 完成；MH-015 待实现 | `app/harness/`、Postgres v12-v14、ADR-011、专项规划 | 33 Experience + 33 Evaluation；0 Draft；required-case evaluator/Promotion Evidence/transition/bounded policy contract；16/16 真实 Postgres contract |
| 开源 Agentic RAG 差距审计 | 完成 | `docs/OPEN_SOURCE_AGENTIC_RAG_GAP_ANALYSIS.md` | 对照 GraphRAG、LightRAG、KAG、RAGFlow、LlamaIndex、Haystack、Graphiti、Mem0、Letta、Hermes；锁定四类 P0/P1 差距 |
| OpenAI Structured Reflection | 完成（live contract） | Responses parse、Pydantic schema、信号触发、服务端 provenance、deterministic fallback | 本机 `gpt-5.6-sol` 返回 live structured；无持久经验时保守选择 none |
| 外部存储适配器 | 完成（当前 P1 范围） | Qdrant、Neo4j、Postgres ingestion/learning jobs/artifacts/transitions/versioned links、knowledge/outbox | migration v1-v10、旧资产导入、reconciliation、真实 Postgres contract、双 worker 与重启通过 |
| API 与示例 | 完成（P0） | FastAPI、CLI、offline demo | ASGI test 与真实离线 smoke 通过 |
| SSE 流式协议 | 完成（P1 v3） | 幂等 start、持久 event log、SSE cursor/resume、run-scoped tool subscription、显式 cancel | 观察断开不取消、cursor 重放、scope、幂等、刷新恢复和 cancelled 终态合同通过 |
| Agent 工作台 | 完成（P1） | React、TypeScript、Lucide、Markdown、响应式布局 | 构建、桌面/390×844 视觉与界面任务闭环通过 |
| 会话与显式记忆交互 | 完成（v1） | 会话列表/恢复 API、独立 session、草稿、显式记忆、反馈说明、失败重试 | API scope/顺序/幂等合同、真实浏览器新建/切换/刷新恢复通过 |
| 会话管理与聊天附件 | 完成（v2） | 持久标题/归档元数据、管理菜单、最多 5 个附件、durable ingestion 状态、历史附件标签 | API scope/归档合同、真实浏览器重命名/归档/恢复/附件入库、桌面与 390 x 844 通过 |
| 首次设置、会话搜索与运行反馈 | 完成（v1） | Persona onboarding、命令面板历史搜索、heartbeat/完成耗时元数据 | production build、桌面/390 x 844、历史恢复和 633 ms 真实问候通过 |
| 长任务活动时间线 | 完成（v1） | 中文阶段/工具投影、实时耗时、停止状态、按 retryable 重试 | 离线真实工具增量流、provider busy、主动停止、桌面/390 x 844 通过 |
| 长任务刷新恢复 | 完成（v2） | 服务端执行 owner、`run_events.jsonl`、cursor backlog、前端 running run 恢复、服务端取消 | 隔离 10 秒慢任务完成刷新→继续→完成与刷新→停止；2 次输入仅 2 条 trajectory，移动端无溢出 |
| 个人事务交互闭环 | 完成（v3） | 快速记录、跨视图定位、日历、归档聚合、到期通知中心、已读/延后/改期重置 | 生产 Compose 完成提醒创建→已读→跳转→延后→改期→归档；桌面/390 x 844 无溢出 |
| 运行与学习资产 UI | 完成（P1） | Runs、Graph、Memory、Skills、Learning Log、Evidence Inspector | 对应 scoped API 已覆盖集成测试 |
| Durable 文档入库与知识库 UI | 完成（P1 + v3 corpus） | 8 类文件、Postgres job、lease/retry/cancel、Document IR、层级 token chunk、原子 chunk replacement、上传/队列/归档 UI | 单元/API 测试、528 篇续传迁移与真实并发 smoke 通过 |
| Qdrant Hybrid Retrieval | 完成（P1 + sparse IDF） | named dense/sparse、IDF modifier、server RRF、payload index、scope filter、shadow migration、stale point cleanup | 43,903-point active collection、Vision 完成后的 57-case gate 和真实容器校验通过 |
| Neo4j 证据图谱适配器 | 完成（P1 candidate semantic + revision-safe structure） | structural graph、候选实体/关系、review gate、evidence join、archive、断点结构重投影 | 528 Document、43,872 active Chunk/HAS_CHUNK，旧 32,129 结构归档；contract 与真实 Neo4j 通过 |
| 图谱候选控制面 | 完成（P1 + evidence reconciliation） | 稳定候选 ID、JSON 审计仓、review event、批准/拒绝 UI、replacement/revision 治理 | 失效 pending 在 JSON/Neo4j 同步归档，approved/rejected/review history 保留，二次 dry-run 为 0 |
| OpenAI 结构化图谱抽取 | 部分完成（v6 gate 通过，backfill 进行中） | Responses API Pydantic schema、window map-reduce、保守 ontology、pending/evidence 门禁 | v6 合同 5/5、自然 arXiv 18/18；当前 28/528 篇完成，697 entities、337 relations，5 篇 timeout 待重试 |
| 图谱抽取质量门禁 | 完成（v6） | 14 个 arXiv 来源、6 category、3 difficulty、tag slices、required cases、原子 JSON | 5-case 与 18-case 均通过；报告冻结 8,165/28,940 total tokens 和延迟 |
| 跨文档实体归并 | 完成（P1 deterministic） | resolver proposal、v1→v2 审计迁移、`same_as` 投影、级联撤销、归并 UI | 单元/contract/API、真实三跳图路径和归档验收通过 |
| 容器与迁移 | 完成（Hermes migration + learning v10） | app/Hermes/Postgres/Qdrant/Neo4j Compose、版本化 job migration、health check、persistent volumes | 五服务健康；Hermes plugin/toolset 与 native admin 实机通过，`8643` 未映射宿主 |
| 图片/Vision Knowledge Object | 完成（首条纵向闭环） | 图片校验、Responses Vision、视觉区域块、原图 API、区域 Evidence Inspector | 真实论文图片上传到 Agent 回答闭环通过；区域坐标、scope、hash 和引用均可审计 |
| Vision 抽取质量门禁 | 完成（11-case v4） | 8 类合成压力图、3 张真实 arXiv 页、required 注入/空白 case、usage/latency/slice/重试报告 | `gpt-5.6-sol` 11/11 API；10/11 严格 case，summary 0.977，其余质量指标 1.0 |
| arXiv 计算机语料同步 | 完成（有界 connector + 528-PDF corpus） | `app/sources/arxiv.py`、CLI、原子 manifest、来源合同 | 777 个候选版本、528 篇唯一 PDF、`1,059,247,539` 字节逐文件校验；528 active 文档、43,872 chunks |
| arXiv PDF OCR/文本化 | 完成（文本层 + Vision + IR） | `app/sources/arxiv_ocr.py`、CLI、按页 Markdown、`DocumentIR`、原子 OCR manifest | 528/528、11,023 页：10,995 文本层 + 28 Vision OCR；168,531 blocks、0 unresolved、0 失败 |
| Agentic Retrieval Controller | 完成（有界 v3） | 严格计划、显式意图约束、原查询锚定、模型 fallback、LangChain 并行补检、usage/trace/UI | deterministic 与真实 `gpt-5.6-sol` controller 均 57/57、MRR 0.911；OpenAI 55 plans + 2 deterministic fallbacks |
| Agentic RAG 冻结基线 | 完成（设计冻结）/实施延期 | `docs/AGENTIC_RAG_LOCK.md` | 完成代码与数据事实审计；判定为有界、证据优先的 Agentic RAG v1，并锁定 `RAG-001` 至 `RAG-010` |
| 自然 arXiv/个人 Retrieval Gate | 完成（57-case v2 + 528-doc v4） | 分类/难度切片、Qdrant eval backend、source-root metrics、原子 JSON | Vision 完成版 57/57、Recall@20 1.0、MRR 0.8924、P95 34 ms；全部类别与难度切片通过 |
| 生产 Embedding 校准器 | 完成（能力）/受阻（live） | 隔离 collection 重建、分批 usage、成本参数、baseline diff、MRR 防退化门槛 | 当前 Key 对 `text-embedding-3-small` 返回 `model_not_available`；0 points 写入，未切换 |

## 已确认决策

- 产品以研发团队 Engineering Intelligence 为默认业务主线，个人学习是同一内核中的通用模式；
  两者共享 Hermes、检索、图谱、Memory、Skill、Evidence 和治理边界。
- 528 篇 arXiv 论文作为个人公共参考层保留，默认不进入企业工作区、企业首次演示和企业黄金题集。
- Hermes Agent 0.19.0 是唯一在线主循环；OpenAI Python SDK 继续负责 Responses、Vision、Structured Outputs、Embeddings 和 hosted Web Search；OpenAI Agents SDK 不再安装或装配。
- LangChain 作为贯穿项目的衔接层，而非仅作为 RAG helper。
- 第一重点 DomainPack 固定为计算机科学，核心合同仍保持可插拔。
- 自进化采用双通道：Hermes 原生 Memory/Skill 保存写前快照后先应用、再审计，可在 after-hash 未漂移时确定性回滚；HermesGraph 高影响资产继续先评测后晋级。
- MemoHarness 只作为 HermesGraph 的经验归纳与固定化控制面：Hermes 独占原生 Memory/Skill；
  逐案例经验 `E` 不改变行为，全局模式 `G` 先评测后晋级，run overlay 只消费已批准模式并在启动时冻结。
- 当前检索系统正式定义为“有界、证据优先的 Agentic RAG v1”；RAG 策略和代码开发按
  `docs/AGENTIC_RAG_LOCK.md` 暂时冻结，只有用户明确恢复 `RAG-*` 工作项时继续。KG backfill
  可以作为数据维护独立续跑，但不得改变冻结架构或抬高产品宣称。
- 用户提供的本机 OpenAI-compatible GPT-5.6 endpoint 用于在线 Agent 与图谱抽取；先过 eval 再切默认 extractor。

## 当前风险

1. Hermes 是唯一在线 Agent；LangChain 只能组织中间能力，OpenAI Python SDK 只能调用模型 API。代码和配置门禁必须继续阻止第二 Agent Loop 回流。
2. “自学习”容易退化成无限追加 Prompt 或保存聊天记录。
3. 计算机公共论文可能淹没用户私人资料；检索必须保留 private/public 分层、来源权重和可见标记。
4. 当前 deterministic dense encoder 只适合开发回放；生产语义质量必须使用真实 embedding 和离线检索集校准。
5. OpenAI structured-output extractor 已通过 18-case/14-source 自然 arXiv 摘要门禁，但仍未覆盖完整 PDF 长上下文、跨文档隐式别名和开放关系本体；一次 provider 长尾使 P95 达 29.96s，后台抽取必须保留 timeout、重试和 durable job。
6. Vision 已通过 11-case/13-region 门禁，覆盖截图、图表、架构图、扫描页、提示注入、近空白图和 3 张真实 arXiv 页；仍未覆盖 PDF 自动选页、开放世界照片、视觉原生 embedding、跨页图表和更多语言/低质扫描分布。最终 P95 为 74.62s，视觉入库必须继续走 durable background job，不能阻塞在线对话。
7. “尽可能多下载论文”若无预算会占满磁盘并给 arXiv 造成不当负载；必须使用官方机器接口、速率/字节/数量预算和增量游标，完整 corpus 只走官方 bulk channel。
8. 当前 retrieval gate 包含 5 个受控 case 和 57 个自然/个人 case，已覆盖同义改写、比较、硬负例、scope 与视觉，但仍不能代表 43,872 chunks 的全部开放查询分布；deterministic encoder 的 1.0 Recall@20 不能外推到生产 embedding。
9. 57-case OpenAI planner v3 controller gate 已完成，但 2/57 请求收到 provider `InternalServerError` 并走确定性降级；生产必须保留 fallback，并监控 provider-only success rate、P95 和 token。
10. 当前兼容端点的模型清单没有 embedding 模型，`text-embedding-3-small` 实测返回 `model_not_available`；在获得可用 OpenAI embedding 凭据前，不能把 deterministic encoder 的成绩外推为生产语义质量。
11. 当前 Skill offline replay 已实际执行声明式步骤，但能力结果来自冻结历史 fixture，不会重新调用模型、网络或真实写工具；它适合可重复非退化门禁，不能外推为开放世界 provider/tool 反事实结果。
12. Learning artifact、Skill 状态、transition ledger、versioned artifact link 与阶段 checkpoint 已在 v9/v10
    合并为受 fencing 保护的 Postgres stage transaction，并通过异常、lease 变化和缺失资产 contract。
    剩余 exactly-once 边界只有外部模型响应到 reflection commit；生产前仍需扩大为逐 stage
    操作系统级强杀与恢复故障矩阵。
13. Web Search v1 固定集已覆盖时效性、一手来源、引用、冲突、多语言、安全与 provider 长尾；
    6 个离线 contract 全部通过，但当前兼容端点最小 live case 在有界重试后仍返回 HTTP 503。
    所以生产必须保持禁用或降级，直到 7 个 live case 的 provider-only success、citation coverage、
    source precision、P95 和 token usage 达到门槛。
14. Hermes 原生 Memory/Skill 是先应用后审计，仍可能短暂学入错误偏好；后端已有精确快照、哈希并发
    前置条件、接受/回滚、append-only 失败审计、retention/GC 和容量健康。专用 UI 仍待完成，但已按
    当前“先后端”决策延期；生产备份恢复演练仍需执行。
15. 当前模型网关已让 v6 extractor 通过 5-case 与 18-case 全门禁，固定 12 并发的前两批分别
    13/20、15/20 成功；累计 28 篇完成、5 篇 unresolved，失败均为 `APITimeoutError`。backfill 必须
    继续复用 Docker `/data` checkpoint，监控批次成功率并重复收敛 timeout；真实 Hermes 纵向验收
    也仍需补跑。
16. 公开 `/v1` API 当前仍采用本地单用户信任模型；内部 Hermes Bridge 有 bearer token，但 run、
    memory revoke、Skill transition、graph review 和 document archive 尚无统一终端用户认证/scope
    middleware。该项是参考项目对比后确认的 API-free P0。
17. Personal Control Plane 已补齐 Task/Plan/Note、Persona、Day Archive 和 Emotion；剩余风险是
    未来把 Hermes native Todo 与 durable Task 做隐式双写。当前明确保持两者独立，任何同步都必须
    增加稳定 idempotency key、来源和冲突策略。
18. 528 篇 computer-science 文档的 43,872 个新版 Chunk 已进入 Postgres、Qdrant 和 Neo4j 结构层；
    旧 32,129 个 Neo4j Chunk/HAS_CHUNK 以及引用旧证据的 12,920 个实体、925 条语义关系、4,989 个
    跨文档归并均已归档。当前 active 语义实体/关系仍为 0；GraphRAG active filter 会正确返回空语义图，
    不能把“结构图谱对齐”描述为“528 篇 v6 实体关系已抽取完成”。
19. MemoHarness 已有 Experience Bank、Pattern miner、Pattern Evaluation、Promotion Evidence、
    append-only transition、observe/shadow、人工 Canary/Active 和 bounded consumer。第一版只消费
    capsule memory limit/confidence、retrieval profile/subquery/round 和 graph hop；生产 Pattern
    Bank 当前仍为 0 Draft，且 MH-015 health/auto rollback 未完成，不能宣称无人监督闭环已成熟。
20. 当前系统满足 Agentic RAG 的核心行为定义，但还不是生产级 Agentic GraphRAG。主要差距是
    deterministic dense 仍承载主索引、active 语义 KG 未完成、required-term gap 不是硬约束、
    Publisher 不做 claim-evidence entailment、没有学习型 reranker。Hermes 0.19 首发/正常收尾/
    原生 review 已真实通过，最终 completion 握手 live 重验受 provider 429 阻断。对外描述必须保留
    “有界 v1”和这些事实边界。

## 2026-07-29 Agentic RAG 冻结与成熟度审计

- 对 `app/retrieval/agentic.py`、`app/retrieval/pipeline.py`、`app/graph/toolkit.py`、
  `app/agent/hermes_bridge.py`、`app/evidence/publisher.py`、当前数据规模和固定评测做了事实审计。
- 结论锁定为“有界、证据优先的 Agentic RAG v1”：已有 typed plan、多查询并行混合检索、
  evidence gap、有界第二轮、GraphRAG 工具、partial failure trace 和服务端引用发布门禁。
- 单独建立 `docs/AGENTIC_RAG_LOCK.md`，记录普通 RAG 对比、模块成熟度、代码映射、真实评测、
  不变量、冻结期非目标、`RAG-001` 至 `RAG-010`、生产 DoD 和严格恢复顺序。
- RAG 策略与代码开发从本检查点起延期；后续 Agent 完善优先进入 MemoHarness observe-only
  基础、终端用户鉴权、持久 Run event 和 Hermes backup/restore。
- KG backfill 仍可复用 `/data/graph_backfill_manifest.json` 做幂等数据维护；pending candidate
  不会因冻结而自动变成 active graph fact。

## 2026-07-29 ChatTutor / Desktop-Claw 用户功能复核

- 重新下载并检查两个仓库当前 `main` 源码，只比较用户可感知功能；测试、容器、迁移、审计、
  证据链、限流、回滚和可观测性不计入覆盖率。
- HermesGraph 已覆盖并超过两者的通用 Agent Loop、知识库、Agentic RAG、在线 GraphRAG、
  长期 Memory、Skill 激活和本机文档读取，但不能宣称完整包含两个项目。
- 本轮已补齐通用 Task/Plan/Step/Checklist/Note、Persona/Onboarding、Day Archive/Diary/Calendar、
  自然语言 Memory 纠错和 Emotion；均已接入 scoped API、Hermes tools 和工作台。
- ChatTutor 仍独有专用苏格拉底教学状态机、学习时长和 Learner Profile；Desktop-Claw 仍独有
  Electron 悬浮球、快速输入、桌面拖放和跨平台安装包。
- Desktop-Claw 的文件创建、修改和删除是有意不包含的高风险能力；HermesGraph 保持受控只读，
  不把安全边界差异误记为必须补齐的功能缺陷。
- 详细逐项状态已更新到 `docs/REFERENCE_PROJECT_COMPARISON.md` 的 4.1 节。

## 2026-07-28 MemoHarness 固定化记忆规划

- 详细阅读论文方法后，决定采用其 `B_t=(E_t,G_t)` 双层经验银行、成功/失败分离检索、D1-D6
  诊断、全局模式蒸馏、正确性优先排序和单次测试时适配思想，不照搬自由 harness rewrite。
- 锁定唯一所有权：Hermes 继续写原生 Memory/Skill；HermesGraph 只消费 trajectory 与脱敏 native
  ChangeSet，形成 Experience、Governed Memory/Skill/Pattern 和冻结 overlay，不回写或复制原生资产。
- 固定化分成 Semantic Memory、Procedural Skill、Harness Policy 三路；Episodic Experience 不直接
  变为永久指令。所有高影响产物继续执行 replay、shadow、canary、人工晋级和自动 rollback。
- 新专项规划记录 D1-D6 可学习字段及禁区、领域模型、Postgres v12 表、durable checkpoint、API、
  feature flags、存量回填、测试矩阵、SLO、DoD 和 `MH-001` 至 `MH-020` 实施依赖。
- 第一实现迭代限定为 `MH-001` 至 `MH-008`：只建 schema、Experience Bank、deterministic diagnosis、
  durable stage、reconciliation 和存量回填，不使用模型 API，也不改变在线 Agent 行为。

## 2026-07-28 KG Backfill Runner 与离线 Tokenizer 修复

- 新增 `scripts/run_kg_extraction.sh`，固定 12 文档并发、默认 20 篇 pilot、复用 Docker `/data`
  checkpoint，并显示完成/失败、实体/关系、耗时和 ETA。macOS Bash 3.2 空数组兼容问题已修复。
- 前两批分别完成 13/20 和 15/20；checkpoint 当前有 33 个 current-revision entry，其中 28 completed、
  5 error，累计 697 个实体候选和 337 条关系候选。5 个错误全部为 `APITimeoutError`，没有 schema、
  Postgres、候选仓或 Neo4j 错误。
- 第三批启动前暴露 `tiktoken` 运行时下载 `o200k_base` 时 Azure Blob TLS EOF。根因不是 Compose 或
  KG API，而是新镜像没有 tokenizer cache，完整 `build_components()` 在构造 ingestion chunker 时联网。
- 将官方 expected hash 为 `446a9538...a2d`、大小 3,613,922 字节的 cache object 固化到
  `assets/tiktoken/`；Docker 设置 `TIKTOKEN_CACHE_DIR` 并在 build 阶段调用 `get_encoding` 校验。
- 新镜像已在 `--network none` 下成功加载 `o200k_base`；KG dry-run 完整启动成功，发现 528 篇、
  跳过 28 篇 completed 并选择下一批 20 篇，不调用模型。运行时不再依赖 Azure Blob。

## 2026-07-28 Vision、结构图谱与候选 revision 治理收口

- 按 manifest 只补跑剩余 25 个低文本页。最终 528/528 文档、11,023 页全部完成：10,995 页使用
  PDF 文本层、28 页使用 GPT Vision OCR，`unresolved_low_text=0`、失败为 0；Document IR 共
  168,531 blocks，其中 167,487 native text、1,044 Vision OCR。
- 只对受 Vision 影响的 7 篇论文强制重切，旧 642 chunks 替换为 664，项目总数从 43,850 增至
  43,872；活动 Qdrant collection 同步为 43,903 points（含 `default` 31）。检索报告
  `.data/evaluations/arxiv_retrieval_v4_vision_complete.json` 为 57/57、Recall@20 `1.0`、
  MRR `0.8923977`、P95 `34 ms`。
- `openai-graph-extraction-v6-window-map-reduce:c6000:n4:o1:gpt-5.6-sol` 修复类型漂移后，
  5-case 合同门禁与 18-case/14-source 自然 arXiv 门禁均全部通过；实体、关系、类型和 evidence
  指标均为 `1.0`，分别消耗 8,165 与 28,940 total tokens。
- 新增 checkpointed `GraphStructureReindexService` 和 `hermesgraph-reindex-graph-structure`。不构造
  model client，从 Postgres 对 528 篇执行 Neo4j 重投影：528/528 成功、43,872 chunks、0 error；
  当前 active Document/Chunk/HAS_CHUNK 为 528/43,872/43,872，旧 32,129 Chunk/关系归档。
- 新增候选 replacement/revision 治理：同文档新批次只 supersede 旧 pending，保留
  approved/rejected；resolver 不再读取 archived/rejected endpoint。新增
  `hermesgraph-reconcile-graph-candidates`，按当前 retained chunk 一次扫描 JSON 审计仓和 Neo4j，
  已同步归档旧证据对应的 12,920 entities、925 relations、4,989 resolutions；13 条 review event
  未删除，二次 dry-run 六项均为 0。
- 20-document v6 pilot 先后以 20,000 和 5,000 字符预算尝试，共享网关均持续 timeout，已安全停止；
  `.data/graph_backfill_manifest.json` 保留 1 个 error checkpoint。恢复时先在 app 容器 `/data` 内跑
  20 篇 pilot，不复用主机侧分叉的候选仓，再按成功率、P95 和 token 预算决定全量并发。
- 最终回归为 254 collected、241 passed、13 个环境型 skip；Ruff 全绿，139 个应用源码 strict mypy
  通过。app 镜像重建后五服务保持运行，维护命令已在真实 Docker `app_data` 与 Neo4j 上完成幂等验收。

## 2026-07-28 Document IR、v3 Chunk 与 IDF 混合索引全量迁移（阶段基线）

- 先用 `--text-only` 重新生成全部 528 篇 OCR sidecar，全程没有构造模型 client。528/528 均升级为
  `document-ir-pdf-v1`，source hash 全部唯一且匹配；共 167,545 blocks，527 篇恢复标题层级。block
  包含 80,366 headings、71,486 paragraphs、7,120 captions、3,011 tables、4,138 list items、
  992 equations 和 432 references；167,539 blocks 来自原生文本层，6 blocks 来自历史 3 页 Vision OCR。
- 新增 `HierarchicalDocumentChunker` v2。它按 section 和 heading path 切分，以 `o200k_base` 严格
  计数，并把相邻短 section 有界打包到最小目标 80 tokens、最大 400 tokens；metadata 保留
  section IDs、heading paths、source block IDs、页区间、OCR 方法、confidence 与 warning。全量结果
  为 43,850 chunks，其中 8,126 个是短 section 打包结果；22,542 个位于 251-400 tokens，19,840 个
  位于 80-250，最大值为 400。
- 新增可续传 `KnowledgeRechunkService`、CLI 和 `.data/knowledge_rechunk_manifest_v2.json`。迁移执行
  source hash/parser revision 校验、进程锁、逐文档 checkpoint、Qdrant 先写与 stale point 删除、
  Postgres `replace_chunks` 原子替换。528/528 成功、0 error；发出的
  `knowledge.document.rechunked` 仅用于审计，不触发 KG backfill。
- 第一个 64,240-chunk v1 结果暴露大量过短 chunk，未直接晋级。v2 packing 后在旧 raw-TF sparse
  collection 上先后只有 53/57 和 52/57；失败报告全部保留，没有删除 case 或降低阈值。根因定位为
  大语料下原始词频放大高频项，而非 Document IR 丢失目标内容。
- Qdrant adapter 增加 collection-level sparse `Modifier.IDF`、schema 校验和陈旧 point 清理；新增
  通用 `hermesgraph-reindex-knowledge` CLI。从 Postgres 将 `computer-science` 的 43,850 chunks 与
  `default` 的 31 chunks 重建到 shadow collection `hermesgraph_chunks_v3_idf`，共 43,881 points，
  再通过 `.env` 成对切换 collection 与 IDF 开关。旧 collection 保留为回滚点。
- 最终只读报告 `.data/evaluations/arxiv_retrieval_v3_idf_complete.json` 为 57/57、Recall@20 `1.0`、
  MRR `0.8850877`、P95 `38 ms`；28/28 fact、15/15 paraphrase 及 comparison、hard negative、personal、
  scope、visual 全部通过。相对历史 MRR `0.9113` 下降约 `0.0262`，低于 `0.05` 防退化阈值，且
  Recall 与 case pass 无回归。该结论仍只适用于 deterministic dense + IDF sparse，不外推为生产 embedding。
- 该阶段结束时 25 个 `unresolved_low_text` 页面仍在 Vision 待补队列，且没有执行 Neo4j 重投影；
  二者已在同日后续“Vision、结构图谱与候选 revision 治理收口”阶段完成。
- 最终回归为 251 collected、238 passed、13 个环境型 skip；Ruff 全绿，135 个应用源码 strict mypy
  通过，Compose config 和镜像 `pip check` 通过。新 app 镜像已运行，五服务健康；overview 报告活动
  collection/IDF 为 `hermesgraph_chunks_v3_idf/true`。Postgres 独立查询为 528 documents、43,850
  chunks、单一 parser revision；Qdrant 为 green、43,881 points、sparse modifier `idf`，两个新 CLI
  均已在容器内验证可调用。

## 2026-07-27 全库图谱状态审计与模型恢复探测

- 磁盘与 OCR manifest：777 个候选版本中有 528 篇唯一 PDF 成功提交，528/528 完成文本化；
  共 11,023 页、39,828,275 字符，25 个低文本页仍待 Vision 增补。
- Postgres/Qdrant/Neo4j 结构层：`computer-science` 有 528 个 active Document、32,129 个 active
  Chunk 和 32,129 条 active `HAS_CHUNK`；文档检索语料完整。
- 语义候选审计仓：12,904 个实体候选覆盖 528 篇文档，908 个关系候选覆盖 87 篇文档，
  4,988 个跨文档归并候选覆盖 521 篇文档；该项目全部 18,800 个候选均为 pending，0 个 review。
- OpenAI revision 只覆盖 28/528 篇：704 个实体候选和 807 个关系候选；其余 500 篇只有
  `rule-entity-relation-v1` 实体候选。Neo4j 中 Entity、SEMANTIC_RELATION、ENTITY_RESOLUTION
  分别保持 candidate 状态，当前 active 语义实体/关系为 0。
- 新凭据对 `http://127.0.0.1:55523/v1/models` 鉴权成功，可见 `gpt-5.6-sol` 等模型。
  `openai-graph-extraction-v4:gpt-5.6-sol` 的 5-case Structured Outputs 重验 5/5 请求成功，
  消耗 7,639 tokens；实体/关系 precision、recall 与 evidence accuracy 均为 1.0，但 required
  prompt-injection case 将 `signed copper key` 标成 `Technology`，而固定集只接受
  `Concept/Product`，所以总 gate 正确地保持失败。报告：
  `.data/evals/graph_api_revalidation_20260727.json`。
- 在修复类型稳定性、重过 5-case 与 18-case 门禁，并实现可续传、幂等、按 revision 记账的图谱
  backfill job 前，不直接对 528 篇论文发起无界批量调用；模型输出仍只能进入 pending review。

## 2026-07-24 参考项目复审与 API-free 后端补强

- 下载并逐文件审计 ChatTutor 与 Desktop-Claw 当日 `main` 源码；新增
  `docs/REFERENCE_PROJECT_COMPARISON.md`，明确区分 README 声明、代码事实、HermesGraph 已有能力、
  部分能力、缺失能力和刻意非目标。
- ChatTutor 的在线知识图谱只做会后构建/展示，不进入回答链；其“自学习”主要是规则画像，没有
  Skill 版本/评测/晋级/回滚。它真正领先的是任务、计划、笔记和教学产品形态。
- Desktop-Claw 有真实 10 轮 ReAct-like loop、六层 Persona Prompt、按天归档和文件工具，但无
  GraphRAG/向量 RAG；后台 interpret、memory correction、脚本 Skill 安全和持久任务恢复仍有明显
  边界。它真正领先的是桌面 Companion、首次引导和日记连续感。
- 新增 `app/computer/workspace.py`：显式 root alias、固定 tenant/project、只读 list/read/search，
  支持文本/代码/PDF/DOCX/XLSX。阻断绝对路径、`..`、隐藏/凭据文件、私钥后缀和全部 symlink；
  使用 `O_NOFOLLOW`、文件/页数/ZIP 解压/扫描/输出预算。读取与搜索生成 run-scoped
  `workspace_file` EvidenceRef，可由严格 publisher 引用。
- 新增 Hermes 插件工具 `list_workspace_files`、`read_workspace_file`、
  `search_workspace_files`；Integration Runtime 使用 `computer:read` capability scope，Bridge
  使用独立 per-run computer budget。标准 Compose 只读挂载仓库 `workspace/`，不挂 HOME。
- 修复 governed Skill “评测完成但在线未消费”的断点：`RuntimeCapsuleProvider` 接入
  `HermesAgentRuntime`，run start 注入同一 snapshot 的相关 Memory/Skill index；新增
  `activate_governed_skill`，只返回钉住的 Canary/Active 精确版本声明式步骤。激活事件进入
  trajectory，health gate 同时兼容历史 `activate_skill`。
- safe read action allowlist 扩展到 GraphRAG、Web、Memory 和 Workspace 工具；任意脚本、Shell、
  SQL/Cypher 和写工具仍不能被 Skill miner 学入。
- 最终离线回归：242 collected，229 passed、13 个环境型 skip；Ruff 全绿，128 个应用源码 strict
  mypy 通过，Compose config 通过。app/Hermes 镜像本轮均成功 build；Docker Desktop 恢复后，
  App、Hermes、Postgres、Neo4j 均 healthy，Qdrant running。App/Hermes health 与真实
  `/v1/capabilities` 冒烟通过，三个 Workspace capability 已在容器中注册；未调用到期模型 API。

## 审计修正记录

- P0 在线工具改为只读；memory/skill 候选由 `run.completed` 后台流程产生。
- DomainPack 从后期迁移能力前移为 Phase 0 核心合同。
- 新增 `CapabilitySpec`、`RunSnapshot`、`LearningChangeSet`，用于跨框架、回放与学习审计。
- P0 不启用 handoff 或 nested specialist，先验证单一根 Runner。
- 所有检索和图谱调用统一经过 `IntegrationRuntime -> CapabilityRegistry`，不允许 Agent 直连 driver。
- Capability 输入和输出均执行 JSON Schema 校验；LangChain tool 必须显式声明 effect。
- 检索结果执行强制 tenant/project 二次过滤，分支失败进入 trace 而不是污染结果。
- Memory 和 Skill repository 均按 tenant/project 隔离，Skill miner 对能力采用 fail-closed allowlist。
- 图谱只接受模板和参数，不接受自由 Cypher；图节点、关系和路径均为 typed contract。
- Skill canary 使用 run/skill 的确定性桶，不会在全量流量中意外激活。
- Run start 冻结 Skill 版本，Snapshot、capsule 与 activation 只能读取同一钉住版本。
- retrieval、graph、memory、skill tool output 均受相同字节预算约束。
- 每个被接受的 Memory 和 Draft Skill 都写入确定性 `LearningChangeSet`，包含来源、风险和回滚条件。
- 移除 FastAPI TestClient deprecation 路径，测试无 warning。
- 前端只调用 scoped workspace API，不直接读取 `.data` 或存储实现。
- 流式 endpoint 保留原有阻塞 endpoint，避免破坏 CLI 和已有调用方。
- Memory 撤回与 Skill 晋级仍经过原有 gate/state machine，UI 不能绕过控制平面。
- Skill transition API 不再接受调用方上传 evaluation；评测必须由服务端生成、持久化并按 skill/scope/version 校验。
- Skill 身份由 tenant/project、稳定触发语义和动作模式决定，不再包含不断增长的来源 run ID；已存在版本不可被后续挖掘重新覆盖为 Draft。
- Shadow 只允许无副作用 projected observation，在线执行入口只接受 Canary/Active；Canary/Active 健康分母只计入实际 `activate_skill` 的钉住版本运行。
- 前端依赖使用 package lock 与精确版本，`npm audit` 为 0 vulnerabilities。
- 上传采用有界读取；文件名规范化、存储路径 traversal guard 和 tenant/project 双重过滤已覆盖。
- 用户上传知识默认标记为 `uploaded_document/user_asserted/private`；受治理连接器可以提交结构化来源合同，arXiv 固定为 `arxiv/observed/public_reference`。归档只移出检索，不破坏审计原文和索引记录。
- `Settings(...)` 不读取工作区 `.env`，只有应用入口 `get_settings()` 读取，避免 CI 和单元测试被开发配置污染。
- Qdrant scope filter 同时施加在 dense/sparse prefetch、主 Query API 和应用层结果校验，防止 RRF 跨项目泄漏。
- Neo4j 的关系不存嵌套 evidence map；只存 `source_chunk_ids`，查询时在同 scope 的 Chunk 节点回连并组装 `EvidenceRef`。
- ingestion 协调 Qdrant 与 Neo4j 写入；任一索引失败会补偿归档所有后端并把本地文档标为 `failed`。
- 内置 lexical branch 使用绝对相关性阈值，LangChain 融合使用 branch weight 和相对阈值，避免主知识库为空时用弱相关资料凑答案。
- deterministic dense 只用于离线回放；Qdrant 候选必须额外通过非停用词词项交集门槛，避免唯一无关候选被 server RRF 提升为有效证据。生产 OpenAI embedding 不启用该限制。
- 语义抽取只能产生 `pending` candidate；所有 active 关系必须经过 `GraphCandidateService`，并将 review event、reviewer、原因和原始 Chunk 证据分开保存。
- 关系批准联动两端实体；实体被拒绝时级联拒绝关联关系，实体归档后 fail-closed 阻止关系晋级。Neo4j 只作为可查询投影，当前 JSON 审核仓未来通过 Postgres/outbox 替换。
- 实体归并不会原地改写或删除文档作用域实体。resolver 只生成稳定 `resolution` 候选；批准后创建 `same_as` 证据边，拒绝端点或归档任一来源会级联撤下该边，保留原实体和审核历史。
- 模型抽取把文档 chunk 明确视为 untrusted data，使用 OpenAI Responses `parse` 与 Pydantic 严格 schema；模型返回的每个证据 ID 必须属于当前 document batch，拒答、未完成和无 parsed output 均失败关闭，并由既有 ingestion 补偿流程撤下半成品索引。
- 抽取评测把实体名称、类型、关系、Chunk 证据和执行成功率分开计分；scope 错位与非 pending 输出是不可被平均分抵消的硬失败，标记 `required_pass` 的安全/负样本也必须逐项通过。
- ingestion job 的 durable 状态只由 Postgres repository 管理；worker 通过 `FOR UPDATE SKIP LOCKED` 领取、owner-bound lease 与 heartbeat 续约，不能依赖进程内队列假装可靠。
- 同 scope/content hash 的并发提交使用事务 advisory lock 与 partial unique index 合并；上传内容先原子写入 scope-hashed staging，再提交 job，数据库失败会清理孤立 staging。
- 当前异步控制面不等于全项目已经 Postgres 化。trajectory、change-set、knowledge metadata、graph candidate 审计仍有本地 repository，后续必须通过逐个 adapter 和 migration 替换，不能绕过现有 contract。
- hosted Web Search 不直接挂在根 Agent；它先经过 `IntegrationRuntime -> CapabilityRegistry`
  的 `web:read` 边界，再把 provider URL annotation 转成当前 run 的 `EvidenceRef`，确保最终回答
  继续使用同一 `AnswerPublisher` allowlist。
- Web 查询发网前阻断疑似密钥；返回端拒绝私网/userinfo URL，并二次执行 domain allowlist。
  `action.sources` 可为空，所以 citation annotation 是发布事实源；无 citation 的 provider 摘要被丢弃。
- Publisher 现在把引用存在性与 provenance trust 分开：untrusted Web evidence 可以形成
  supported claim，但不能形成 verified claim/confidence。

## 2026-07-22 GraphRAG Tool Suite 验收

- 新增四级图谱工具：`resolve_graph_entities`、`retrieve_evidence_subgraph`、
  `compare_graph_entities` 与低层 `search_graph`；Hermes 提示已写明工具路由，关系+文本问题不再先
  重复调用普通检索。
- `GraphEntityResolveRequest/Result`、`GraphRAGRequest/Result` 与实体对比合同进入领域层；
  `GraphRetrievalToolkit` 在 LangChain Integration Runtime 内并行执行文本召回和实体解析，再做有界
  1-3 hop 扩展。
- 本地与 Neo4j resolver 统一 canonical/alias/type 确定性评分；Neo4j 在多个 mention 中按最高分选择，
  只读取 active Entity 并 join 同 scope active Chunk evidence。
- 联合子图在 adapter 校验后再次要求 node/relationship scope 一致、每条关系 evidence 非空且路径
  必须包含已解析 seed node ID；真实 Neo4j 烟测因此识别并剔除 1 条同名 Chunk 结构路径，只保留
  `AURORA-VAULT-8301 protocol -[requires]-> blue seal...` 语义路径。
- Qdrant 与 Neo4j 返回同一来源时优先按稳定 `chunk_id` 去重，再回退到 provenance identity，避免
  两套 backend 的 source ID 格式差异把同一 Chunk 重复暴露给 Agent。
- 实体对比由 node ID 集合运算产生连接路径、共享和左右独有邻居，不让模型猜测拓扑；真实 Compose
  返回 1 条 `requires` 连接路径，0 个伪共享结构邻居。
- Capability Registry 为联合工具同时要求 `graph:read + knowledge:read`，Hermes Bridge 增加
  `MAX_GRAPH_TOOL_CALLS`、重复输入检测、ToolEvent 和本轮 evidence allowlist；插件四个图工具 schema
  已有独立 contract test。
- 新增 REST API：`/graph/entities/resolve`、`/graph/retrieve`、`/graph/compare`；五服务重建后
  app/Hermes/Postgres/Neo4j healthy，Qdrant running，三接口使用真实 Neo4j/Qdrant 数据通过。
- 全量回归：236 collected，223 passed、13 个环境型 skip；Ruff 全绿，126 个应用源码 strict mypy
  通过。在线 Hermes 模型 E2E 仍按既有决策等待新凭据，不混入本轮完成声明。
- 最后加入的“多 mention 最高分选择”和“跨库稳定 chunk ID 去重”已通过单元/contract 与只读真实
  Neo4j 验证；本机审批通道中断并拒绝了最终镜像重建，因此当前运行容器是上一轮已通过三接口烟测
  的版本，下一次正常 `docker compose up -d --build app hermes` 会带入这两项源码更新。

## 2026-07-22 arXiv 300 篇扩容与 API-free 处理

- 在原 228 篇基础上通过官方 Atom metadata 和 canonical PDF 链接新增 300 篇 LLM/Agent 相关论文；
  单篇 10 MB、批次 1.8 GB、串行 1 秒节流、重试、原子 manifest 和断点续传继续生效。同步读取
  750 个候选，写入 300 个唯一 PDF、`548,976,688` 字节，0 duplicate；23 个超预算候选与 1 个
  返回 404 的版本保留审计状态，不占成功下载额度。
- 最终 manifest 有 777 个版本：528 `submitted`、225 `metadata`、23 `skipped_oversize`、1 `error`。
  对全部 528 个文件独立重读 `1,059,247,539` 字节：528 个路径均受控、文件存在、size 匹配、
  `%PDF-` magic 正确、SHA-256 匹配且 hash 唯一，0 orphan、0 missing、0 corruption。
- 新增 `--text-only` 离线模式：不读取模型配置、不构造 client、不渲染低文本页；低文本页写为
  `unresolved_low_text`，未来恢复 Vision 后只重跑含待补页的文档。最终 528/528 文档、11,023 页、
  10,995 页 PDF 文本、3 页历史 GPT Vision OCR、25 页待 Vision、39,828,275 字符，0 失败。
- 新增 `--submit-pending`：不访问 arXiv，只提交有有效 PDF 且没有 job ID 的条目；第一次提交 494
  成功并识别 6 篇早期 10-13.3 MB PDF 与 10 MB 应用上限不一致。Compose 显式加入 20 MB 上传上限
  后只重试失败项，6/6 成功；本轮 500/500 新 job 最终成功，4 个经历一次自动重试、0 失败。
  连同原有 28 篇，最终为 528 active 文档、32,129 chunks，Postgres/Qdrant/Neo4j 完成且 Outbox 为 0。
- 批量处理真实暴露 `JsonGraphCandidateRepository` 只有进程内锁：第二 Outbox writer 会发生
  read-modify-write 丢更新，并让 resolution 引用刚被覆盖的实体。仓库已增加同目录 `flock` 跨进程
  事务锁，并保留原子替换语义；新增双进程真实文件回归，修复后再次用双 worker 处理真实队列，
  同类错误未复现。本轮不执行任何 live eval，遵守“当前无模型 API，先完成 API-free 工作”的决定。
- 全量规则抽取一度生成 112,983 条 JSON pending resolution，Neo4j 历史投影达到 125,360 条；其中
  `LLM` 一项就有 54,953 个两两组合，属于 O(n²) 候选爆炸而非有效知识。resolver 升级为 v2，每个
  新实体只连接一个最佳代表；repository 将未审核 resolution 压缩为高置信边优先的最小森林，所有
  非 pending 记录原样保留。JSON 最终为 4,988，Neo4j 删除 120,372 条冗余 pending 投影后同为
  4,988；候选 API 默认每类最多返回 500 条，并用响应头返回 12,904/908/4,988 三类总数。

## 2026-07-15 产品需求重新对齐

- 北极星从“泛领域通用研究 Agent”收敛为“面向计算机/AI 的自进化多模态个人 Agent”。科研资料仍可使用，但产品中心变为个人知识、个人任务、长期记忆和可控能力积累。
- 当时的 OpenAI 能力主线固定为 Responses API、Tool Calling、Structured Outputs、Vision、Embeddings/Vector Search，以及确有需要时的 Background Tasks；Agents SDK 是该阶段在线编排 adapter，不进入领域合同。2026-07-20 起在线 adapter 已由 Hermes 取代。
- Agentic RAG 的完成语义明确为意图判断、查询分解/改写、多源检索、证据缺口、补检、验证和引用回答；固定一次检索不算完成。
- 第一重点 DomainPack 固定为计算机科学，优先覆盖 Agent、RAG、知识图谱、长期记忆、多模态、工具调用、自进化和软件工程。
- arXiv 作为公共参考知识层：小规模主题检索走 Atom API，大规模元数据增量走 OAI-PMH，完整 corpus 只走官方 bulk channel；PDF 本地缓存只用于个人检索并保留 canonical 回链、版本和 license metadata。
- 用户提供的本机兼容端点已完成 `/models`、`/responses` 和 `/chat/completions` 探测，实际可用模型 ID 为 `gpt-5.6-sol`。共享 provider adapter 已接入 Agent 与图谱 eval；密钥没有进入源码、示例、文档或镜像。
- 首次 live v1 报告协议与 evidence/scope 全部成功，但 entity precision `0.786`、relation precision `0.556`、relation recall `0.714`，required prompt-injection case 未通过。系统没有降低门槛或直接上线。
- v2 将关系类型收敛为受控计算机/研究 ontology，并禁止把用途短语、时间/条件尾句抽成独立实体或复合谓词。第二次 live 报告 5/5 case 通过：11 个实体、7 条关系零误报零漏报，实体/关系 precision、recall、类型与 evidence accuracy 均为 `1.0`，P50/P95 为 `9.99s/10.83s`，总计 `7,625` tokens。报告保存在 `.data/evals/graph_gpt_5_6_sol_conservative_v2.json`。
- 因 golden set 仍只有 5 个初始样本，v2 只获准作为本机 `openai` pending candidate extractor，不能自动晋级 Neo4j active 关系；下一步必须用 arXiv 计算机语料扩展自然分布评测。
- 当时多模态仅为锁定设计；后续已完成图片原件、Vision 严格 schema、视觉派生块、文本查图、区域证据、scope 隔离、原图 hash 回读和失败补偿的首条纵向验收。该历史决策保留用于说明范围演进，不代表当前状态。

## 2026-07-15 Postgres durable 异步入库验收

- 新增 `IngestionJob`、状态枚举和 repository contract；公开状态为 `queued/running/retry_scheduled/succeeded/failed/cancelled`，内部 `staging_key` 和 `lease_owner` 不进入 API 响应。
- `PostgresIngestionJobRepository` 使用 lazy `asyncpg` pool、版本化 migration 表、作用域索引、active-content partial unique index、事务 advisory lock 和 `FOR UPDATE SKIP LOCKED`。启动迁移失败会关闭半初始化连接池。
- worker 为每次领取绑定 owner lease，按 lease 三分之一周期 heartbeat；完成和失败更新均校验 owner。进程丢失后 lease 可回收，超过最大次数转 terminal failed，避免永久 running。
- 文档损坏与 staging 缺失按永久失败处理；Qdrant/Neo4j 索引异常和未知基础设施错误按指数退避重试。队列任务可取消，terminal retryable 任务可人工重试；成功后 staging 自动删除。
- API 新增提交、列表、详情、取消、重试五类 scoped endpoint。Postgres 暂时不可用统一映射 503，状态冲突映射 409，scope 外任务表现为 404。旧同步上传 endpoint 保留兼容。
- React 知识库在 async 模式显示紧凑任务队列，1.5 秒轮询状态，支持取消和重试；任务完成后自动刷新文档与 overview。桌面显示状态/次数/时间，移动端重排且不增加嵌套卡片。
- Compose 增加 `postgres:17.10-alpine3.23`、本机端口、持久卷和 health check；app 等待 Postgres 与 Neo4j healthy 后启动，默认 `INGESTION_MODE=async`。
- 实机提交 `INTENT.md` 得到 `queued -> running -> succeeded`，只执行 1 次，lease 释放并生成 document ID；migration 表记录 version 1。Qdrant point 与 Neo4j pending candidate 均增长，SSE 返回已绑定 provenance 的证据。
- 两个并发 `PROGRESS.md` 请求返回同一 job ID，分别为 `coalesced=false/true`；只生成一个成功任务。Qdrant 从 4 增至 17 points，后续流式问答返回 3 条直接来自新文档 chunk 的 `uploaded_document/user_asserted` 证据。
- Codex in-app browser 在 `1280x720` 与 `390x844` 验收知识库：桌面队列宽 978 px、移动队列宽 362 px，job row 均未越界；两种视口都满足 `clientWidth=scrollWidth`，移动底栏与内容不重叠，浏览器 warn/error 日志为空。
- 四服务最终 healthy；全量验证为 85 tests、Ruff、82 个应用源码 strict mypy、TypeScript/Vite production build。前端产物 JS 410.40 kB（gzip 123.07 kB），CSS 34.81 kB（gzip 6.91 kB）。

## 2026-07-15 图谱抽取质量门禁验收

- 新增 `GraphExtractionGoldenSet`、case/entity/relation 期望合同、阈值合同、case result 和 aggregate report，全部采用 frozen/extra-forbid Pydantic 模型；空 chunk、未知字段和非法阈值在运行前失败。
- evaluator 对实体名称做规范名称/别名一对一匹配，实体类型单独计分；关系端点支持显式别名，谓词必须属于 case 的 allowlist。误报、漏报、类型错误和错误 Chunk evidence 分开记录。
- 报告输出 micro entity/relation precision、recall、F1、entity type accuracy、evidence accuracy、success rate、P50/P95 latency，并保留每个 case 的预测与失败原因。
- extractor 异常会同时降低 success rate 并把该 case 的全部期望项记为 false negative。错误 tenant/project/document/domain scope、任何非 pending 候选属于硬门禁，不允许由其他 case 的高分抵消。
- `required_pass` 支持安全关键样本逐项阻断。初始 golden set 包含英文架构、中文检索、研究多 Chunk 证据、提示注入和无事实负例，其中提示注入与负例为 required。
- `OpenAIUsageAccumulator` 只累计 input/cached/output/total token，不保存 prompt 或正文。模型价格不硬编码；只有显式提供当次单价才估算美元成本，且价格快照与报告一起保存。
- 新增 `hermesgraph-eval-graph` console script 与 `python -m app.evaluation.graph_cli`，支持 rule/openai/hybrid、阈值覆盖、原子 JSON 报告和 CI 非零退出；`--report-only` 只用于记录未达标基线。
- 离线 rule 基线 5/5 case 执行成功，entity precision/recall/F1 为 `0.778/0.636/0.700`，relation 为 `0.500/0.286/0.364`，evidence accuracy 为 `1.0`；干净负例通过，提示注入 required case 因误报/漏报被正确阻断，报告整体不通过。
- 最终 Docker 镜像内的 `hermesgraph-eval-graph` console script 已实际执行，报告原子写入持久卷 `/data/evals/graph_rule_baseline_v1.json`；容器摘要与宿主基线一致，证明 CLI、golden fixture 和报告模块均随成品打包。
- 全量验证为 78 tests、Ruff、79 个应用源码 strict mypy。真实 OpenAI 报告仍缺失，因此该门禁只证明评测基础设施和 rule 基线，不证明模型质量。

## 2026-07-15 OpenAI 结构化图谱抽取验收

- 新增 `OpenAIStructuredEntityRelationExtractor`，调用 Responses API `responses.parse(..., text_format=StructuredGraphDraft, store=False)`；实体类型、关系命名、字段长度、confidence、输出数量和 source Chunk 均由严格 Pydantic schema 限制。
- system prompt 声明 chunk 是不可信数据而不是指令，禁止跨文档身份解析、补充世界知识或无证据猜测。文档正文只作为独立 JSON user payload 发送，不能进入 system instruction。
- 应用会再次验证模型给出的 `source_chunk_ids` 是当前文档批次的非空子集；伪造/跨批次证据对应的实体和关系直接丢弃。所有合格结果仍固定为 pending，只有 `GraphCandidateService` 可以晋级。
- 新增 `HybridEntityRelationExtractor`，并发运行规则与模型 extractor，按共享 UUID5 identity 合并 aliases、证据、最高 confidence 和 rationale。它是 ingestion 数据流，不创建 LangChain/OpenAI 第二个 Agent Loop。
- `GRAPH_EXTRACTOR_MODE=rule|openai|hybrid` 已接入 Settings、bootstrap、Compose、`.env.example` 和工作台 overview。`rule` 保持默认且无需 key；`openai/hybrid` 缺少 `OPENAI_API_KEY` 时拒绝启动。
- 拒答、`incomplete` 和无 parsed output 均抛出受控抽取错误；OpenAI 客户端最多执行两次基础设施重试，最终失败进入既有 knowledge indexing 补偿路径，文档标为 failed 并归档索引。
- 全量验证为 72 tests、Ruff、77 个应用源码 strict mypy、TypeScript/Vite production build；JS 406.63 kB（gzip 122.58 kB），CSS 32.22 kB（gzip 6.53 kB）。重建后的 app/Qdrant/Neo4j 三服务 healthy，overview 返回 `graph_extractor_mode=rule`，应用日志无错误。
- 该子阶段结束时尚无真实模型凭证，因此当时只证明 API 合同、解析、隔离、融合和失败语义。随后同日完成的 OpenAI v3 live golden 已达到 5/5 和全指标 1.0；该结果仍只代表初始 5-case gate，不代表自然计算机论文分布。

## 2026-07-15 跨文档实体归并验收

- 新增 `EntityResolutionCandidate` 与 `EntityResolverPort`。候选固定携带两端 entity/document ID、规范名称、实体类型、匹配策略、resolver revision、双来源 Chunk、confidence 和审核状态。
- `DeterministicEntityResolver` 只比较同 tenant/project、不同 document、相同 entity type 且未拒绝/归档的实体；支持 `exact_identifier`、`exact_name`、`normalized_name`、`alias_overlap`，不使用模糊编辑距离自动猜测身份。
- 归并 ID 由 scope、排序后的两端候选 ID 和 resolver revision 通过 UUID5 生成；重试、重启和摄取顺序不会制造重复 identity link。
- `graph_candidates.json` 升级为 v2，旧 v1 文件可直接读取；首次写入原子迁移并补空 `resolutions`。重复抽取保留已审核状态，归档后重传保守恢复为 pending。
- `GraphCandidateService` 是唯一晋级门禁。批准归并会联动批准两端实体；拒绝任一实体会级联拒绝相关关系和归并；拒绝/归档端点无法批准归并，所有变化分别写 review event。
- Neo4j 以 `ENTITY_RESOLUTION` 关系保存控制字段、以 `relation_type=same_as` 的查询语义对外投影；只有 approved 映射 active，边证据由两个文档的 active Chunk 在同 scope 回连生成。
- 工作台候选审核新增“归并”视图，展示两端实体/文档、匹配策略、证据数量、置信度、状态和批准/拒绝操作；桌面表格与移动三行布局共用既有审核视觉语言。
- Docker 实机上传两份包含 `ORION-RESOLVE-7715` 的不同文档。关系已批准但归并 pending 时，Qdrant 实体到 Neo4j 实体之间没有跨文档路径；批准归并后得到 `Qdrant <-uses- ORION ->same_as-> ORION -supports-> Neo4j` 三跳路径，三条边证据数为 `1/2/1`。
- 归档任一来源后，`same_as` 立即从 allowlisted path 结果消失，审计候选变为 archived；第二来源仍可保留自己的 `supports` 路径。应用日志全程无错误，三服务最终 healthy。
- 全量验证更新为 63 tests、Ruff、75 个应用源码 strict mypy、TypeScript/Vite production build；JS 406.63 kB（gzip 122.08 kB），CSS 32.22 kB（gzip 6.55 kB）。桌面表格无溢出，390×844 下 `clientWidth=scrollWidth=390`、关键字段完整、浏览器日志为空。

## 2026-07-15 可审核语义图谱验收

- 新增 `GraphEntityCandidate`、`GraphRelationCandidate`、`GraphExtractionBatch`、`GraphCandidateReviewEvent` 和 extractor/repository/index ports，scope、证据和 revision 为必填合同。
- `RuleBasedEntityRelationExtractor` 支持 Markdown heading、代码符号、稳定标识符，以及中英文显式 `requires/uses/depends_on/...` 关系；输出使用 UUID5 稳定去重且永远保持 pending。
- `JsonGraphCandidateRepository` 原子保存实体、关系和 review event；重复抽取保留已审核状态，文档归档统一转为 archived，再上传保守恢复为 pending。
- Neo4j 新增文档作用域 `Entity` 和参数化 `SEMANTIC_RELATION` 投影；pending/rejected/archived 不参与查询，approved 映射 active 并回连同 scope Chunk 生成 `user_asserted` 证据。
- 工作台知识图谱新增“图谱探索/候选审核”模式、实体/关系视图、状态筛选、置信度、来源分块和批准/拒绝操作；移动端候选记录改为三行布局，所有审核操作首屏可见。
- 真实 Docker 文档生成 6 个实体和 1 条 `requires` 关系；pending 时没有语义边，批准后图查询返回 `AURORA-VAULT-8301 protocol -> requires -> blue seal...` 及原文证据，归档后路径归零。
- 通过移动 UI 批准关系后，计数从 `7 pending` 变为 `4 pending / 3 approved`；同页图谱探索返回 2 paths/2 evidence。桌面和 390×844 移动端无页面溢出，浏览器日志为空。
- 全量验证更新为 60 tests、Ruff、74 个应用源码 strict mypy、TypeScript/Vite build、真实 Qdrant/Neo4j 和浏览器交互通过。

## 2026-07-15 Docker 与外部检索纵向验收

- `docker-compose.yml` 现包含完整 `app + qdrant + neo4j` 栈，端口只绑定本机，三者均使用持久卷，应用和 Neo4j 有 health check。
- 应用镜像以 UID 10001 非 root 用户运行；`requirements.runtime.lock` 固定已验收生产依赖，构建执行 `pip check`。
- `scripts/docker_up.sh` 统一完成前端 production build、镜像构建和 Compose 启动；容器内前端与 FastAPI 同源提供。
- Qdrant 使用 `dense`/`sparse` named vectors、两路 filtered prefetch、服务端 RRF、payload 索引和应用层 fail-closed scope/status 复核。
- Neo4j 使用固定 `neighbors`、`paths(1..3)`、`conflicts` 模板；实体、scope、limit 均参数化，不接受自由 Cypher。
- 文档入库同步 MERGE `Document -> HAS_CHUNK -> Chunk`，关系携带 `source_chunk_ids`、confidence、extractor revision 和 scope；归档后路径不可见。
- 真实基础设施脚本验证 Qdrant 写入/检索/归档，以及 Neo4j schema/写入/路径/证据/归档，并自动清理 smoke scope。
- 完整容器应用上传 `AURORA-VAULT-8301` 后，Neo4j API 返回 1 条带原文证据的路径；SSE 返回 1 条 `qdrant_hybrid`、`uploaded_document`、`user_asserted` 引用和 1 项学习变更。
- 归档后相同问题得到 `confidence=insufficient`、0 citation，Neo4j 返回 0 path；重新上传恢复相同确定性 ID 和两套索引。
- 全量验证：56 tests、Ruff、mypy strict、TypeScript/Vite build、npm production audit 0 vulnerabilities、Compose config、runtime `pip check` 全部通过。
- Docker Hub/官方 npm registry 在首次构建时多次 EOF/ECONNRESET；基础镜像改用本机已有的固定 digest，Compose build args 默认使用可覆盖的 PyPI 镜像，前端静态产物由宿主机稳定构建后打入镜像。
- Codex in-app browser 已完成 1280×720 桌面与 390×844 移动验收：三栏桌面布局、移动底部导航、输入区和运行检查器无重叠或横向溢出。
- 首次界面任务暴露 deterministic dense 的误召回：Agentic RAG 问题错误引用唯一上传文档 `Mission Protocol`。加入离线词项门槛和集成回归后，同一问题只返回相关的 verified 项目文档；56 项全量测试保持通过。

## 2026-07-14 知识库纵向验收

- 增加 `KnowledgeDocument`、`KnowledgeChunk`、`IngestionResult` 合同和 `active/archived/failed` 生命周期。
- 本地 ingestion 支持 PDF、Markdown、TXT、JSON、CSV、HTML；使用 LangChain `RecursiveCharacterTextSplitter`，并以原始内容 SHA-256 做 scoped dedup。
- `RetrievalPipeline` 新增动态 `knowledge_base` 分支，与 `builtin_lexical` 并行运行后由现有 LangChain RRF 融合，没有引入第二个 Agent Loop。
- 工作台新增知识库主视图，支持拖放/选择上传、去重反馈、分块/大小/hash 展示和二次确认归档。
- 真实服务上传 `AURORA-VAULT-8301` fixture 后，SSE 返回 `uploaded_document`、`knowledge_base` branch 和 `user_asserted` provenance；归档后同一查询不再返回该来源。
- 全量验证结果：44 tests、Ruff、mypy strict、TypeScript 和 Vite production build 通过；JS 395.94 kB（gzip 120.72 kB），CSS 26.40 kB（gzip 5.76 kB）。
- 浏览器插件在清理会话后重试仍报 `Cannot redefine property: process`，因此知识库视图尚缺桌面/移动截图验收；该限制不影响 HTTP、API、SSE 和构建验证。

## 2026-07-14 前端与流式验收

- 单一 FastAPI 地址同时提供工作台、OpenAPI 与 API；生产静态文件由 `frontend/dist` 提供。
- 工作台包含对话、运行、知识库、知识图谱、记忆、技能、学习记录七个主视图。
- 一次实际 SSE smoke 收到 accepted、status、heartbeat、tool、32 个 answer delta、3 个 evidence、learning 和 completed 事件。
- 桌面布局为导航、主工作区、运行检查器三栏；移动端改为底部导航和可开关全屏检查器。
- TypeScript production build 成功，产物 JS 390.61 kB（gzip 118.82 kB），CSS 23.65 kB（gzip 5.34 kB）。
- Codex in-app browser runtime 初始化遇到插件自身的 `Cannot redefine property: process`，因此本轮没有把插件截图作为完成证据；HTTP 静态资源、SSE、ASGI 集成测试与构建均已完成。此项需在浏览器插件恢复后补做视觉截图复核。

## 纵向闭环验证记录

2026-07-13 使用 `/tmp/hermesgraph-smoke` 连续执行三次相同离线任务：

- 3 个 run 均完成并带 evidence citation。
- 写入 3 条 episodic memory，每条保留 source run provenance。
- 写入 4 个 change set：3 个 memory、1 个 skill。
- 挖掘出 `learned_agents_langchain_openai_sdk@0.1.0`。
- Skill 状态保持 `draft`，未进入 canary/active，未影响在线行为。

## 2026-07-15 arXiv Source Connector 与来源合同验收

- 新增 frozen、extra-forbid 的 `KnowledgeSource` 合同，统一记录来源类型、稳定 ID、版本、canonical URI、license URI、private/public_reference、信任级别和采集时间。默认个人上传仍是 `uploaded_document/user_asserted/private`。
- `IngestionJob` 持久化来源对象；Postgres migration v3 `ingestion_job_knowledge_source` 已真实应用并锁定 checksum。worker 把来源原样传入 `KnowledgeDocument`，Postgres 文档元数据、Qdrant payload、Neo4j Document/Chunk 和 citation 共用同一投影逻辑。
- arXiv 论文固定为 `source_type=arxiv`、`source_id=arxiv:{id}`、`source_revision=vN`、`privacy=public_reference`、`trust=observed`；citation source ID 增加 `#chunk=N`，并保留 canonical 回链。缺失 license 保持 `null`，不伪造开放许可。
- 新增官方 Atom API connector，支持分类/主题查询、100 条以内分页、版本化 ID、作者/分类/日期/DOI/journal/comment/license/link 解析、明确 User-Agent、超时、429/5xx/Retry-After、指数退避和请求间隔。
- PDF 下载只接受 HTTPS arXiv host，先检查 Content-Length、再流式检查实际字节，验证 `%PDF-` magic；限制单篇、单次总字节和下载数量。文件名规范化、路径 containment、原子写入、SHA-256 跨条目去重和原子 manifest 已覆盖离线测试。
- CLI 可只同步缓存，也可把已缓存 PDF 逐篇提交现有 scoped durable ingestion API。manifest 在每篇后落盘，记录 metadata/downloaded/duplicate/submitted/skipped_oversize/error、hash、字节、相对路径和 job ID；重复运行跳过已完成版本。
- 首次真实同步检索 30 条计算机/AI 元数据，成功缓存 28 篇 PDF，合计 `65,867,960` bytes（磁盘约 63 MB）；2 篇超过 10 MB 单篇上限，被记录为 `skipped_oversize`，未放宽应用上传预算。语料包含自进化 Agent、GraphRAG、多跳推理、长期记忆、MCP、coding Agent 和多模态 Agent。
- 28 篇缓存论文已提交到独立 `computer-science` 项目，28/28 durable jobs 成功并形成 1,496 个 chunks；默认个人项目保持隔离。Qdrant 搜索可返回 canonical arXiv citation，图谱语义抽取通过 Outbox 解耦执行，pending 候选仍不能绕过审核门禁。
- PDF/文本解析增加页数、提取字符和 chunk 三重预算，并移到 worker thread，避免批量 PDF 阻塞事件循环；永久不可重试的无效文档会清理 staging，保留可人工重试的基础设施失败对象。
- 离线全套测试、Ruff、91 个应用源码 strict mypy 通过；真实 Postgres 3 个 contract 通过。新镜像启动后 migration v1/v2/v3 完整，旧知识仍为 3 active/2 archived、15 active chunks，Outbox unpublished 为 0，运行态为 `openai/gpt-5.6-sol/postgres/qdrant/neo4j/openai-extractor`。

## 2026-07-15 在线 Agent 严格输出与引用门禁验收

- OpenAI Agents SDK 的 `output_type` 从包含完整任意 citation 字典的 `AnswerResponse` 改为严格 `AgentAnswerDraft`。模型只能选择本轮工具返回的 `citation_ids`，服务端 `AnswerPublisher` 再从 evidence allowlist 补全 URI、来源合同、页码和视觉坐标；未知 ID、claim 未包含于 citations、无证据高置信结论均失败关闭。
- 修正容器 session SQLite 默认路径，使其显式落在 `/data/sessions.db`，不再依赖不可写的工作目录。
- 真实 `computer-science` 查询 run `30b7d487-ee5a-4388-86e6-fcb1ff302378` 成功：回答生成 6 个带证据 claim、9 个 citation，来源均为带 chunk 定位的 arXiv canonical URI 和 `observed/public_reference` 合同。

## 2026-07-15 Vision 多模态纵向验收

- 图片入库支持 PNG、JPEG 和 WebP。Pillow 在调用模型前验证实际格式、扩展名、解码、单帧、像素和尺寸预算；原始 bytes 原样保存，`GET /v1/projects/{project_id}/documents/{document_id}/content` 以私有缓存和 `nosniff` 返回。
- `OpenAIVisionAnalyzer` 使用 Responses API `parse` 和严格 `VisionAnalysis` schema，生成 title、summary、visible text 与最多 40 个归一化区域。模型把图片当作不可信数据，不做身份或敏感属性推断；派生 revision 固定为 `openai-vision-knowledge-v1:<model>`。
- 图片总览和区域被转换为可检索 chunk；Qdrant、Neo4j structural projection 与 `EvidenceRef` 携带 `modality=image`、region ID 和 `[x,y,width,height]`。前端 Evidence Inspector 可回看原图并叠加本轮 citation 的区域框。
- 真实 MemOps 论文图片以 async job `bd0a774b-631d-43ab-b99e-3a369dd752dd` 成功入库为文档 `f99b03fb-d7b7-5472-bbfd-9d31581ba66d`：原图 927x1200、269,211 bytes、内容 hash 回读完全一致，生成 10 个视觉区域和 16 个 chunks。
- 真实视觉问答 run `b6cde082-90b3-4c92-87cc-839e7b6617a7` 成功，回答只依据图片可见摘要并明确限制，引用 region-05 等 3 个视觉区域；region-05 坐标为 `[0.113,0.342,0.887,0.702]`，未虚构图片中不可见的 probe 名称。
- 全套 pytest（3 skipped）、Ruff、93 个应用源码 strict mypy、前端 production build 均通过。当时此验收只证明首条链路可运行；后续多类型视觉 golden gate 已在 2026-07-16 完成，PDF 自动选页与图片原生 embedding 仍待实现。

## 2026-07-16 Agentic Retrieval Controller 验收

- 新增严格 `QueryPlanDraft`，覆盖 `lookup/compare/synthesis/personal_recall/visual_lookup`、最多 4 个首轮/备用查询、required terms、最少证据/来源、视觉要求和图搜索建议。OpenAI planner 使用 Responses API Structured Outputs，query 作为不可信 JSON 数据；它不读取召回正文，也不能回答问题或生成自由图查询。
- 新增 deterministic planner 作为离线回放基线和在线受控降级。OpenAI timeout、429、refusal、`incomplete`、空 output 或 schema 错误只在 trace 中记录异常类型，随后继续执行确定性计划，不把 provider 错误伪装成模型质量通过。
- `AgenticRetrievalController` 通过 LangChain `RunnableLambda.abatch` 并行执行最多 4 个 subqueries；底层保持 Qdrant dense/sparse hybrid 与强制 scope，控制器再执行 cross-query RRF、稳定 evidence 去重和 partial-failure 聚合，不创建第二个 Agent Loop。
- 每轮固定计算 `RetrievalGap`：证据数、按 document/source root 归并后的独立来源数、视觉证据数、required term 覆盖和缺口原因。最多运行 2 轮，停止原因只能是 `coverage_satisfied`、`no_new_queries` 或 `round_limit`；OpenAI Agent 同一 run 最多调用控制器 3 次。
- controller trace 已进入 `ToolEvent.detail`，但不复制 evidence 正文。工作台 Run Inspector 可显示 intent、轮次、停止原因、planner revision、执行 queries 与最终缺口；桌面和 390x844 移动 DOM 均无横向溢出，应用页浏览器日志为空。
- 新增 retrieval golden schema/evaluator/CLI，指标包括 Recall@K、MRR、forbidden source、intent、轮次、新增证据、停止原因和 planner fallback。受控 deterministic v1 的 5 个 case 覆盖私有标识符、比较、图片检索、scope 隔离和 hard negative，最终 5/5、平均 Recall@K 1.0、MRR 1.0、1 个二轮 case、0 planner fallback、0 scope decoy。
- 初次基线真实暴露两个问题：句首 `Compare` 未被确定性 intent 匹配，hard negative 被低分 stopword 误召回；修复比较判定并提高 fixture 的相关性门槛后才达到 5/5。该数据集是控制器 contract baseline，不是生产检索质量证明。
- 真实 Docker offline acceptance run `9a2d8fc8-ec36-4303-9a0e-2540cd7dd166` 查询 MemOps 图片：1 次工具、1 轮、5 条证据、2 个独立来源、4 条视觉证据、required terms 全覆盖，以 `coverage_satisfied` 停止并返回区域引用。验收后 `.env` 与 Compose 已恢复 `openai/gpt-5.6-sol` 在线模式。
- 审计保留失败 run `0e828b23...`：一次 helper 移位导致 gap assessment 返回 `None`，API 以 `AttributeError` 失败。已修正函数边界并增加 `test_gap_assessment_always_returns_a_strict_result`；失败轨迹不删除，用于证明错误可观察而非静默吞掉。
- OpenAI planner live probe 已到达用户提供的兼容 endpoint，但 provider 返回 `429 model_cooldown`。真实容器中的相同路径按设计记录 `RateLimitError` 并降级，随后仍从 Qdrant 返回 7 条证据（其中 5 条视觉证据）、1 轮覆盖完成。在线 planner 的计划质量和成本报告仍待冷却结束后补跑。
- 最终全量回归已通过：pytest 全套完成（3 skipped）、Ruff、96 个应用源码 strict mypy、前端 production build、npm production audit 0 vulnerabilities、Compose config 和 deterministic retrieval report 5/5。最终镜像已重建，app/Postgres/Neo4j healthy、Qdrant running，`/health` 返回 200；overview 保持 `openai/gpt-5.6-sol/qdrant/neo4j/postgres/openai-extractor`，9 runs、6 memories、1 skill、4 documents、31 chunks、active ingestion/outbox 均为 0。1280x720 最终工作台无横向溢出、运行详情可见 `visual_lookup / 1轮 / coverage_satisfied`，浏览器日志为空。

## 2026-07-16 自然 arXiv Retrieval Gate

- `hermesgraph-eval-retrieval` 新增 `--backend fixture|qdrant`。Qdrant 模式只读当前 collection，复用与应用一致的 deterministic/OpenAI dense adapter、hashed sparse、scope filter 和 controller；报告通过同目录临时文件、`fsync` 与原子 replace 写入，异常不会留下半份 JSON。
- evaluator 将 `arxiv:{id}#chunk=N` 规范到论文 source root 后再计算 Recall@K、MRR 和 forbidden hit，避免同一论文多个 chunk 被误当作多个 ground-truth 来源；新增 distinct source、单 case latency、mean/p95 latency 指标。没有 expected target、只验证 forbidden source 的 scope case 不参与目标排序惩罚。
- 新增 `examples/evaluation/arxiv_retrieval_golden.json`：基于本地 manifest 摘要与已入库 PDF 人工核对 12 个自然 case，覆盖 PalmClaw、MemOps、EvoGraph-R1、SLEUTH、RAGU、OpsMem、ToolAtlas、AgentCheck、WebDesignIter、双论文比较、无答案拒答和 default/computer-science 项目隔离。
- 首次真实 Qdrant baseline 为 11/12：10 个正向/比较 case 均召回，scope decoy 为 0，但 hard negative 因旧门槛“任意一个词重合即可”被 `rule/apply` 等单个泛词放行，返回 8 个 chunks 并错误 `coverage_satisfied`。该失败报告被后续报告覆盖，但结果已在本节保留，不把首次失败抹去。
- 当时的 deterministic lexical gate v1 将带数字/连字符的专有标识符设为精确命中，并要求无标识符长查询至少两个非停用词重合。后续 57-case 回归发现普通连字符复合词会被误判，最终 v2 已收紧为“长度至少 4 且含数字”；完整修正记录见下一节。
- 修复后自然 baseline 为 12/12，Recall@20 `1.0`、MRR `0.9444`、1 个二轮 case、0 planner fallback；AgentCheck 目标位于第 3，其他正向 case 首个目标位于第 1。hard negative 返回 0 evidence，经两轮后 `round_limit`；跨项目 case 对目标 arXiv 来源的 forbidden hit 为 0。宿主全量并发回归报告 mean `364.5 ms`、p95 `1394 ms`；最终容器内打包 CLI 为 mean `22.25 ms`、p95 `125 ms`。两者环境和并发条件不同，延迟只作运行记录，不作为跨环境性能承诺。
- 该门禁仍只是 12-case 的自然子集，且当前 collection 使用 deterministic 256 维 encoder。它证明真实语料合同、词法/向量融合和 scope 行为，不证明生产语义 embedding、开放式改写、引用覆盖或 OpenAI planner 质量；provider 的 retrieval planner live gate 仍因 `429 model_cooldown` 保持待完成。
- 本阶段最终全量 pytest（3 skipped）、Ruff、96 个应用源码 strict mypy、5-case fixture gate、12-case natural gate、前端 production build 和 Compose config 全部通过。镜像内 `hermesgraph-eval-retrieval` 已实际执行并把原子报告写入 `/data/evals/arxiv_retrieval_deterministic_v1.json`；app/Postgres/Neo4j healthy、Qdrant running，overview 仍为 `openai/gpt-5.6-sol/qdrant/neo4j/postgres/openai-extractor`。

## 2026-07-16 57-case Retrieval Gate 与弱点修复

- v2 数据集扩展到 57 条唯一 case：28 个逐论文事实定位、15 个困难同义改写、5 个跨论文比较、3 个 hard negative、2 个 scope isolation、3 个个人知识查询和 1 个视觉查询；难度分布为 easy 1、medium 33、hard 23。评测报告新增 category/difficulty 聚合、pass rate、MRR、Recall、独立来源数和 P95。
- 首次真实 Qdrant 运行是 51/57，保留了 6 个真实弱点：`personal`/`image` 子串造成两个 intent 误判，两个虚构标识符被通用词放行，UMoE/Vinci2 被同源分块挤到第 12/13 位。修复过程没有删除 case 或降低阈值。
- 一次中间修复把所有连字符复合词误当作硬标识符，结果回归到 44/57；该规则已撤销并增加回归测试。最终规则只把长度至少 4 且含数字的 token 作为硬标识符，普通 `self-improving`、`egocentric-video` 仍走常规词法门槛。
- deterministic planner v2 用词边界识别个人/视觉意图，避免 `autonomy` 命中 `my`；`Compare A with B` 稳定拆成两个并行子查询，并要求至少两份独立来源。5 个真实比较 case 最终全部召回两侧来源。
- `KnowledgeSource` 增加规范标题；重复内容可通过 scoped `enrich_source` 幂等富化 Postgres/本地元数据并重建索引。`--refresh-submitted` 只读取本地 manifest/PDF，不访问 arXiv；28/28 缓存论文以 deduplicated job 成功回填，0 errors，原 acquired time 保持不变。
- Qdrant 对标题与分块正文共同编码；每个查询取最多 `2×top_k` 候选后先排列不同 document/source 的首条证据，再回填同源分块，避免长论文垄断前排。UMoE 首个目标从第 13 提到第 2，Vinci2 从第 15 提到第 9。
- 最终 deterministic 报告 `.data/evals/arxiv_personal_retrieval_deterministic_v6.json` 为 57/57、Recall@20 `1.0`、MRR `0.9113`、4 个二轮 case、0 planner fallback、mean `288 ms`、P95 `572 ms`。同一镜像在应用容器内复跑仍为 57/57，mean `27.98 ms`、P95 `46 ms`，报告写入容器持久卷 `/data/evals/arxiv_personal_retrieval_deterministic_container_v1.json`；两组延迟只用于各自运行环境内的回归比较。7 个类别和 easy/medium/hard 三个切片全部 100% 通过；hard negative 均返回 0 evidence，scope forbidden hit 为 0。
- provider 冷却解除后，真实 `gpt-5.6-sol` Structured Output planner 的受控报告 `.data/evals/retrieval_agentic_openai_v1.json` 为 5/5、Recall/MRR `1.0`、0 fallback、mean `6.62 s`、P95 `8.80 s`。该结果证明在线 planner 合同和基础质量，不代替 57-case 自然分布的在线成本/质量门禁。

## 2026-07-16 OpenAI Planner 全量门禁与 Embedding 校准器

- Retrieval evaluator 增加 planner usage 与逐 case 计划审计：逻辑请求数、返回 usage 的请求数、input/output/total tokens，以及实际 intent、首轮 subqueries、fallback queries、required terms 均进入原子 JSON。provider 失败仍单独记录 `planner_fallback_error`，不会被 controller 总通过数抹平。
- OpenAI planner v1 的首次自然门禁为 54/57、Recall@20 `1.0`、MRR `0.9119`。失败不是召回丢失：两个明确包含 `my uploaded` 的查询被模型标成 `lookup`，另一个 Double Ratchet 查询被改写后目标从第 1 降到第 6；另有 2 次 provider 500 走 deterministic fallback。该失败报告保留在 `.data/evals/arxiv_personal_retrieval_openai_planner_v1.json`。
- v2 在 prompt 与服务端增加显式个人/视觉/比较意图约束，并把原查询与模型改写同时放入首轮。它达到 57/57，但平均 MRR 降到 `0.8313`，平均执行查询数从 deterministic 的 `1.16` 增到 `2.18`；虽然 case 阈值全过，仍因排序退化未晋级。报告保留在 `.data/evals/arxiv_personal_retrieval_openai_planner_v2.json`。
- v3 将简单查询首轮固定为原查询，模型改写进入有缺口时的二轮 fallback；比较查询继续稳定拆分，并强制至少两条证据和两个独立来源。最终 `.data/evals/arxiv_personal_retrieval_openai_planner_v3.json` 为 57/57、Recall@20 `1.0`、MRR `0.911306`，与 deterministic 基线相同；4 个 case 进入二轮，mean `7.50 s`、P95 `16.0 s`，平均执行查询数 `1.28`。
- v3 共发起 57 个 planner 逻辑请求；55 个成功返回 usage，累计 44,878 input、10,501 output、55,379 total tokens。`personal_memops_visual` 与 `personal_progress_storage` 两次收到 `InternalServerError` 并由 deterministic planner 接管，所以结论是“controller 57/57、provider plan 55/57”，不是 provider 自身 57/57。
- `OpenAIDenseEmbedder` 现在支持有界分批、顺序恢复、维度/索引校验和并发安全 usage 累计；OpenAI-compatible 部署可复用 `MODEL_BASE_URL/MODEL_API_KEY`，官方 OpenAI 仍使用 `OPENAI_API_KEY`。`hermesgraph-calibrate-embeddings` 会按数据集 scope 从 Postgres 读取 active 文档，幂等写入显式的新 collection，拒绝覆盖活动 collection，并输出索引失败、embedding usage、可选成本、57-case 结果、逐 case baseline regression 和 MRR 最大下降门槛。
- 真实 1024 维隔离探测在首个文档的 49 chunks 前失败关闭：兼容端点返回 `model_not_available`，其 `/models` 清单只有 GPT/Image 等模型，没有 embedding 模型。报告 `.data/evals/embedding_calibration_openai_te3s_1024_probe_v1.json` 为 `passed=false`、0 documents/0 chunks indexed、evaluation missing；活动 `hermesgraph_chunks` 未修改。该结果是凭据能力阻塞，不是生产 embedding 通过。
- 最终全量 Ruff、98 个应用源码 strict mypy、pytest（3 skipped）和前端 production build 全部通过；新镜像安装了 `hermesgraph-calibrate-embeddings`。app/Postgres/Neo4j healthy、Qdrant running，`/health` 返回 200；最终容器内 deterministic 57-case 仍为 57/57、Recall@20 `1.0`、MRR `0.911306`、mean `12.88 ms`、P95 `17 ms`，报告写入 `/data/evals/arxiv_personal_retrieval_deterministic_post_planner_v3.json`。overview 明确保持 `openai/gpt-5.6-sol/qdrant/deterministic-embedding/neo4j/openai-extractor/postgres`，没有误切到失败的 OpenAI embedding collection。

## 2026-07-16 自然 arXiv 图谱抽取门禁

- 新增 `graph_extraction_arxiv_golden.json`：18 个 case 来自 14 篇计算机 arXiv 论文，包含 16 条自然事实、1 条自然负例和 1 条叠加提示注入的论文事实；覆盖 architecture/method/evaluation/multi_chunk/security/negative 六类、easy/medium/hard 三档，以及 agent memory、tool use、knowledge graph、multimodal、self evolution 等标签。
- golden contract 增加 `source_id/source_title/source_uri`、category、difficulty 和标签；来源字段必须成组出现，evidence index 必须属于 case chunk，关系两端必须存在于期望实体，case ID 必须全局唯一。report 增加 category/difficulty/tag 切片以及逐 case `source_id`，空 slice 仍按明确的零分母语义计算。
- 首轮 `openai-graph-extraction-v3:gpt-5.6-sol` 报告保存在 `.data/evals/graph_arxiv_gpt_5_6_sol_v1.json`，18/18 API 成功但门禁失败：entity precision `0.8182`、relation precision `0.7727`、entity/relation recall `0.9730/0.8947`。8 个实体误报、5 个关系误报、1/2 个实体/关系漏报；自然负例错误产生 `model selection part_of agent`，required case 阻断生效。
- v1 审计暴露两个实现弱点：名称清洗会把 `GraphRAG-Bench (Medical)` 的右括号误删；提示词允许孤立概念、把框架类型声明推成 `implements`、把上下文介词推成 `part_of`，且没有统一 `builds/maintains -> contains`。修复保留平衡括号，并将 v4 本体收敛为“每个实体必须是返回关系端点”、禁止上述推断、明确研究谓词归一化。
- 同一份未改动 v1 数据集复测生成 `.data/evals/graph_arxiv_gpt_5_6_sol_v2.json`：18/18 case、37/37 entities、19/19 relations 全部命中，零误报零漏报，entity/relation precision/recall/F1、type accuracy、evidence accuracy 均为 `1.0`；6 个 category、3 个 difficulty 与全部标签切片均通过。首轮已经通过的 11 个 case 无退化，required 负例与提示注入分别通过。
- v2 使用 26,686 input、5,528 output、32,214 total tokens；P50 `8.08s`，P95 `29.96s`，长尾来自 `arxiv_umoe_outperforms_direct_sft` 单次调用。该成绩允许 v4 继续作为 pending candidate extractor，不改变人工审核后才能进入 Neo4j active 图的治理边界，也不能外推到完整 PDF 或开放本体。
- 图谱 CLI 报告写入已改为同目录临时文件、flush/fsync 和原子替换；括号保真、prompt contract、数据集分布与非法 evidence/endpoint 均有回归测试。
- 最终 Ruff、98 个应用源码 strict mypy、全套 pytest（3 skipped）、前端 production build、Compose config 和 npm production audit（0 vulnerabilities）均通过。新镜像 `pip check` 无损坏依赖；app/Postgres/Neo4j healthy、Qdrant running，工作台 `/health`、OpenAPI、overview 与容器内图谱 CLI 均通过，overview 保持 `openai/gpt-5.6-sol/qdrant/deterministic-embedding/neo4j/openai-extractor/postgres`，未越过 embedding 或图谱审核门禁。

## 2026-07-16 Vision 多模态抽取门禁

- 新增 `vision_golden.json` 与 11 个冻结 SHA-256 的 PNG 资产：8 个确定性合成压力图覆盖架构图、柱状图、表格、Agent 工作台、扫描事故笔记、多区域代码/流程图、提示注入和近空白负例；3 个真实页面来自 RAGU 与 ToolAtlas arXiv 论文。数据集覆盖 9 个 category、3 个 difficulty、13 个期望区域，注入与空白 case 为不可被平均分抵消的 `required_pass`。
- 新增 `VisionGoldenSet`、`VisionEvaluator`、`hermesgraph-eval-vision` 和原子 JSON 报告。门禁分别计算调用成功率、严格 case 通过率、title/summary/OCR/warning、区域召回/类型/自包含文本、bounding-box IoU、禁止内容、区域预算、P50/P95、token 与 category/difficulty/tag slices；资产 hash、来源三元组、相对路径、区域 ID 和期望框均在调用模型前严格验证。
- 首轮 `openai-vision-knowledge-v1:gpt-5.6-sol` 报告 `.data/evals/vision_gpt_5_6_sol_v1.json` 失败：10/11 调用成功、0/11 严格 case，summary/OCR/region/bbox 分别为 `0.795/0.844/0.538/0.667`，10 个样本产生区域预算违规，required 注入请求又遇到 provider 502。该失败没有通过降低阈值或删除样本掩盖。
- 提示词 v2 将图像明确视为不可信证据，要求一个主要视觉对象对应一个自包含区域、区域 OCR 完整、正文段落不碎片化、空白装饰不生成区域，并把图内指令只保留为 OCR/警告而不污染标题摘要。required 注入+空白探针 2/2、全部质量指标 `1.0`；完整 v2 达到 9/11，但 ToolAtlas 遇到一次 502，RAGU 复合 Figure 3 的区域粒度契约仍不一致。
- 数据集 `v1-v3` 均保留为不可变快照；v4 将共享图例/图注的 Figure 3 作为一个复合图区域，并接受图中原词 `Memory Architecture`。提示词 v3 进一步要求摘要保留可见方法/阶段/指标全称，并在多 panel 图中覆盖每个指标。CLI 的重复 `--case-id` 产生带稳定哈希的子集 revision；`--case-attempts` 只重试连接、超时、限流和服务端错误，报告保留 `attempt_count/attempt_errors/model_attempts/recovered_cases`，不会重试 hash、schema 或语义失败。
- 最终报告 `.data/evals/vision_gpt_5_6_sol_final.json` 通过：11/11 调用成功、10/11 case 严格全项通过、0 次恢复；title/OCR/warning/region/category/region-text/bbox/forbidden 均为 `1.0`，summary 为 `0.9773`，P50/P95 为 `15.62s/74.62s`，使用 25,149 input、14,389 output、39,538 total tokens。唯一非全项是 ToolAtlas 摘要完整描述三张内存图与 `M_T`，但没有逐字包含 gold 的总体短语；区域文本含原词，required 安全/空白 case 均全项通过。
- 最终发布回归为 Ruff、100 个应用源码 strict mypy、全套 pytest（3 skipped）、前端 production build（JS 413.39 kB、CSS 35.92 kB）、npm production audit 0 vulnerabilities 和 Compose config 全部通过。新镜像 `pip check` 成功；容器内视觉 CLI 与 `v1-v4` schema 加载通过，app/Postgres/Neo4j healthy、Qdrant running，`/health` 为 200，启动日志无错误，overview 保持 `openai/gpt-5.6-sol/qdrant/deterministic-embedding/neo4j/openai-extractor/postgres/async-ingestion`。

## 2026-07-16 Hermes Skill 自进化闭环

本节保留首次投影回放版本的历史事实；当前实现已由下方 v10 冻结能力沙箱和 SemVer refinement 取代。

- `SkillEvaluation` 升级为版本化系统报告，包含 evaluator revision、作用域、Skill 版本、逐来源 run case、baseline/candidate score、动作序列相似度、工具成功率、unsupported claim rate、安全与非退化结论。公开 transition API 删除 evaluation 输入并保持 `extra=forbid`，调用方不能伪造通过报告。
- 新增 `DeterministicSkillEvaluator`、评测仓和观测仓。安全扫描阻断任意执行动作、未声明 Capability、危险输入键、提示注入片段和非法工具预算；来源 run 缺失或跨 scope 直接失败。当前 replay 是历史轨迹投影，报告明确记录其局限，不冒充完整模型反事实重跑。
- 新增 `SkillEvolutionService`：`observe` 模式保留 Draft；`shadow/canary/active` 学习模式在稳定 Draft 首次产生后自动生成评测，按 `draft -> security_review -> offline_pass -> shadow` 顺序推进。刚生成 Skill 的来源 run 不计入 Shadow 样本，防止先看答案后评测的数据泄漏。
- Shadow 使用匹配查询的无副作用 projected observation；在线 `SkillRegistry.run` 只发现 Canary/Active，Shadow 只能通过显式 `replay` 入口离线执行。Canary/Active 只统计运行开始时钉住对应版本且实际产生 `activate_skill` 事件的观测，曝光未激活不污染健康分母。
- `SkillHealthReport` 返回后端配置的最小样本数、baseline/candidate 均值、unsupported rate、failure rate、曝光和激活计数。Shadow -> Canary 与 Canary -> Active 同时要求健康门禁和人工批准；达到实际激活样本窗口后发生退化会自动转为 `rolled_back`，并写入独立、幂等、带触发 run 的 ChangeSet。
- Skill 身份改为 tenant/project、稳定触发 token 和动作模式的 UUID5，不再把不断增长的来源 run IDs 算入身份。Learning Engine 发现同名同版本资产时保持既有版本不可变，修复了第四次相似运行试图用新 Draft 覆盖 Shadow 并导致后处理失败的问题。
- 新增 OpenAI Structured Reflection：Responses `parse` + `OpenAIReflectionDraft` 严格 schema；轨迹以有界 untrusted JSON 输入。模型只产生 summary、strength/weakness 和 semantic/procedural/none 候选，服务端绑定作用域、run provenance、hash、TTL 和 confidence cap，随后仍经过 MemoryWriteGate。
- 模型反思采用信号触发：显式反馈、非 completed 状态、纠错/审计/安全/失败标签才调用；普通成功 run 使用 deterministic reflection。拒答、incomplete、timeout、连接或 schema 错误自动返回 deterministic 结果，ChangeSet 只记录错误类型链。真实 `gpt-5.6-sol` probe 返回 `live_structured`，无可持久化经验时保守选择 `none`，仅写 episodic memory。
- API 新增系统评测、评测历史和 Skill evolution snapshot；React 工作台显示评测 case、质量变化、门禁状态、Shadow/Canary 样本进度和受控批准按钮。进度分母来自 API 的 `required_observations`，不硬编码部署阈值；回滚与阻断状态均可见。
- 新增纵向测试覆盖自动 Draft -> Shadow、系统报告不可伪造、Shadow 样本门禁、两次人工批准、Canary 健康退化自动回滚、稳定 Skill 身份、Shadow 在线隔离、OpenAI 成功结构化输出和 deterministic fallback。
- 现有 `learned_what@0.1.0` 已在真实容器用 3 个历史来源 run 完成系统评测：3/3 case、baseline/candidate `0.925/0.925`、security/regression 均通过，并按顺序进入 Shadow。随后 3 个新的 `gpt-5.6-sol` Agent 任务形成 3/3 Shadow 观测，failure/unsupported rate 均为 0、`promotion_ready=true`；系统没有自动批准 Canary，保留人工发布门禁。
- 最终发布回归为 164 tests collected、161 passed/3 skipped，Ruff 全绿，108 个 app/scripts Python 源码 strict mypy 通过；前端 production build 为 JS 416.67 kB（gzip 125.03 kB）、CSS 37.20 kB（gzip 7.34 kB），npm production audit 0 vulnerabilities，Compose config 通过。新镜像 `pip check` 成功，app/Postgres/Neo4j healthy、Qdrant running，`/health` 为 200；overview 为 `openai/gpt-5.6-sol/shadow/openai-reflector/qdrant/deterministic-embedding/neo4j/openai-extractor/postgres`。技能页在 1280x720 与 390x844 均无横向溢出，浏览器 warning/error 为 0。

## 2026-07-17 arXiv LLM/Agent 语料扩容

- 用现有官方 Atom API connector 执行两批可续传同步，每批新增 100 篇 PDF，共新增 200 篇。查询限定为计算机分类，并要求同时命中 LLM 与 Agent/Agentic/多智能体/工具使用/计算机使用/自进化/长期记忆主题。
- 第一批下载 `249,557,742` bytes，100 篇成功；8 个最新元数据条目一度因 PDF 镜像 404/500 失败。第二批重试时这 8 个条目全部恢复，并再下载 `194,845,149` bytes、100 篇，0 errors、0 duplicates。
- 当前 manifest 含 314 个版本：28 `submitted`、200 `downloaded`、6 `skipped_oversize`、80 `metadata`。本地实际缓存 228 篇 PDF，逻辑字节数 `510,270,851`，磁盘占用约 488 MB。
- 对 228 个文件独立重读验收：228/228 以 `%PDF-` 开头，manifest byte size 全部匹配，SHA-256 全部匹配且 228 个 hash 互不重复，路径全部位于受控 `pdfs/` 目录，0 损坏。
- 新增 200 篇的主分类以 `cs.AI` 78、`cs.CL` 31、`cs.SE` 29、`cs.LG` 14、`cs.CR` 13、`cs.CV` 11 为主，发布日期范围为 2026-07-09 至 2026-07-16。本阶段只扩容原始语料缓存；新增 200 篇尚未提交 durable ingestion，已入库基线仍为原有 28 篇/1,496 chunks。

## 2026-07-17 arXiv GPT Vision OCR

- 新增 `ArxivOcrProcessor` 与 `hermesgraph-ocr-arxiv` CLI。处理器读取受控 arXiv manifest，只接受 `pdfs/` 内已缓存文件；按页保留提取方式、字符数、GPT confidence/warnings，并以 fsync + 原子替换逐文档写入 Markdown 和 OCR manifest。
- 采用混合策略：先用 `pypdf` 读取数字文本层，去除空白后少于 80 字符的页面才以 180 DPI PNG 调用 GPT Vision。专用 `OcrPageAnalysis` Structured Output 只允许忠实转录、confidence 和 warnings，提示词明确把页面视为不可信证据，禁止执行图中文字、总结、翻译或猜测遮挡内容。
- 全量普查与处理覆盖 228 篇、4,618 页；4,615 页直接使用 PDF 文本层，仅 3 个章节/图页调用本机兼容端点的 `gpt-5.6-sol`，分别为 `2607.12733` 第 20 页、`2607.10275` 第 16 页、`2607.09839` 第 13 页，最终置信度为 `1.0/0.99/0.99`。
- 最终 `.data/arxiv/ocr/texts` 有 228 个 UTF-8 Markdown，OCR manifest 228/228 `completed`、0 error，页标记 4,618/4,618 匹配，累计 `16,798,215` 字符、磁盘约 17 MB。独立复跑为 0 processed/228 skipped，证明 source hash + 输出路径断点续传生效且不会重复调用 GPT。
- 新增低文本页选择、GPT OCR 合并去重、按页输出和幂等恢复测试；定向 pytest、Ruff 与 strict mypy 通过。该阶段生成可检索文本资产，不等于新增 200 篇已经写入 Qdrant/Neo4j。

## 2026-07-19 Postgres Durable Learning Job

- 新增 `LearningJob`、`LearningJobResult`、`LearningJobSubmission` 与 `LearningJobRepository` 合同。状态固定为 `queued/running/retry_scheduled/succeeded/failed/cancelled`，不可变 trajectory snapshot、lease owner 和 fencing token 不进入公开 API。
- Postgres 全局 migration v4 新增 `learning_jobs`，保存 run/feedback 触发器、输入指纹、不可变轨迹、attempt、可用时间、租约、结果引用和失败审计。v5 增加 scoped run partial unique index，从数据库层保证同一 run 同时最多一个 `running` job。相同 trigger+snapshot 通过 SHA-256 幂等键与事务 advisory lock 合并；completion/feedback 按创建顺序执行，避免旧快照并发覆盖新反馈。
- worker 复用 ingestion 的 `SKIP LOCKED`、heartbeat、指数退避、最大尝试、取消和人工重试语义，并额外给每次 claim 生成 UUID fencing token。renew/complete/fail 必须同时匹配 owner+token，过期执行不能提交终态。
- `RunService` 不再在异步模式等待 reflection/evaluation/observation；完成、失败、取消和反馈只保存轨迹并提交 job。Docker 默认 `LEARNING_JOB_MODE=async`，本地默认 `inline`，保持离线开发和旧测试兼容。
- 原 `learn_after_run` 被收敛为 `LearningWorkflowProcessor`，统一执行 Reflection、Memory gate、Skill mining、Observation 和受控 staging。重放时复用已存在的同版本 Skill，修复“Skill 已保存但后续失败后永久停在 Draft”的断点。
- Skill Evaluation 改用 `skill/version/evaluator/实际 replay 输入` 的稳定 UUID5；Observation ID 加入 evaluator revision。Memory 逻辑键、Skill 身份、Evaluation、Observation 和 ChangeSet 均具备逻辑幂等基础。
- 后端新增 scoped learning job 列表、详情、取消和重试 API；越 scope 返回 404，状态冲突 409，Postgres 控制面不可用 503。overview 增加 `learning_job_mode`、总任务和活跃任务计数。本阶段按要求未修改前端。
- 新增单元/API/真实 Postgres contract，覆盖重复提交、反馈新 generation、结果恢复、重试后成功、永久失败、取消/人工重试、scope 隔离、隐藏快照与 fencing 失效。当阶段全套 pytest、Ruff、strict mypy 通过，并完成 migration v1-v5 与 learning lease contract；同日后续已升级到 v7。
- Docker 实机验证了 3 个任务：一个在线回答发布失败的 run 仍由 OpenAI Structured Reflection 学习成功；一个 completed run 由 deterministic reflector 学习成功；随后负反馈生成独立 `feedback_received` job 并调用 OpenAI reflection。3/3 均为首次尝试成功，反馈任务复用既有 Memory/ChangeSet 逻辑 ID，未制造重复资产。
- 这一阶段的边界曾是 Postgres job + 文件 artifact；同日后续的 v6/v7 checkpoint 改造已替换该限制，见下节。

## 2026-07-19 Postgres Learning Artifact 与阶段恢复

- 全局 migration v6 新增 `learning_memories`、`learning_skills`、`learning_skill_evaluations`、
  `learning_skill_observations`、`learning_change_sets` 和 `learning_artifact_imports`。Compose 默认
  `LEARNING_ARTIFACT_BACKEND=postgres`；本地默认仍可选择文件 backend。
- 旧 `memory.json`、版本化 `SKILL.md`、evaluation/observation/change-set JSON 在数据库 migration
  后按规范化内容 hash 生成 import key，只导入一次且不长期双写。真实持久卷导入得到 17 memories、
  1 skill、1 evaluation、3 observations、19 change sets；容器重启后 import ledger 仍为 1。
- 每个 worker artifact transaction 都读取 task-local execution fence，并在同一事务内验证
  `learning_jobs` 仍为 running、owner/token 匹配且 lease 未过期。真实 Postgres contract 证明 job
  完成后旧 fence 不能再写 Memory。
- Memory 精确重放保持 identity/timestamp，已撤销记录不能被后台重试复活。Skill 同 name/version
  只允许 status 更新，immutable definition hash 改变直接冲突。Evaluation、Observation 和
  ChangeSet 使用稳定 UUID + 忽略纯审计时间的语义 payload hash；同 ID 不同内容不覆盖。
- migration v7 给 job 增加不可公开的单调 checkpoint：
  `reflection_completed -> artifacts_committed -> observations_committed -> evolution_committed`。
  `LearningEngine` 拆为 `reflect/apply_reflection`，checkpoint 保存 trajectory hash、评估、模型 revision、
  strengths/weaknesses、动作序列和 Memory candidates；崩溃重试从 artifact 恢复，不重复调用 reflector。
- Skill evaluation fingerprint 排除可变 status/created_at；Memory 与 Skill ChangeSet ID 加入语义
  mutation hash，反馈修订生成新变更记录，普通重试保持同一记录。
- 新增无 HTTP 的 `hermesgraph-worker` / `python -m app.worker` 入口和
  `scripts/check_learning_workers.py`。真实 Compose 中 API worker 与第二个独立容器竞争 40 个任务，
  40/40 首次尝试成功且全部到 `evolution_committed`；独立 worker 日志确认实际领取并完成多项任务。
- 完整 Agent run `377f903f-0c57-4cd7-9ae7-298fe429669b` 生成 job
  `bf99672e-e014-4323-8bdb-33b67965db30`，首次尝试成功并持久化 1 个 Memory、1 个 ChangeSet 和最终
  checkpoint。应用重启后 job/checkpoint 不变，四个 Compose 服务健康。
- 本节记录的是 v7 当时状态；artifact/checkpoint 相邻 transaction 的缺口已由下方 v9 收口。

## 2026-07-19 Skill Transition Ledger

- migration v8 新增 `learning_skill_transitions`，把 Skill 当前状态与状态变化历史分离。ledger 覆盖
  自动 Draft→Security Review→Offline Pass→Shadow、健康门禁拒绝、人工批准/拒绝和 rollback。
- `SkillTransitionEvent` 保存 from/to、allowed、applied、reasons、evaluation ID、human approval、
  learning job ID 与 decided_at；`applied=true` 必须同时 `allowed=true`。Postgres 使用稳定 ID +
  semantic payload hash，旧 worker 写入继续受 job fencing 保护。
- local backend 新增原子 `JsonSkillTransitionRepository`；Postgres backend 新增 facade 和只读
  `GET /v1/projects/{project_id}/skills/{skill_id}/transitions`。legacy importer 升为 v2，可迁移以后
  本地产生的 transition 文件；本次旧库无 transition，因此 v2 marker 的各项导入计数为 0。
- Promotion evaluator 同时校验 skill ID、version 和 tenant/project scope，旧 evaluation 不能跨版本
  或跨作用域推动状态。
- 真实 Compose 对现有 Shadow Skill 请求直接 Active，在 0/5 Canary 样本下被拒绝；Skill 保持
  Shadow，ledger 保存 `health_gate/allowed=false/applied=false`、evaluation ID 与人工批准标记。
  migration 1-8 连续、四服务健康；单元/API 和 Postgres immutable/fencing contract 通过。
- 本节记录的是 v8 当时状态；状态与 ledger 相邻事务的缺口已由下方 v9 同事务 stage commit 收口。

## 2026-07-19 Durable Learning v9 原子 Stage 与 Reconciliation

- migration v9 新增 `learning_job_artifact_links`、`reconciliation_status` 和错误摘要；checkpoint
  同时保存 transition IDs，最终 result 必须与 checkpoint 的 Memory、ChangeSet、Skill、
  Observation、Evaluation 和 Transition 引用完全一致。
- `PostgresLearningTransaction` 使用 task-local context 让同一确定性 stage 内的所有 learning
  repository 复用一个 asyncpg connection/transaction。最终 checkpoint 更新重新验证 owner、
  fencing token 和未过期 lease；失败时 artifact、Skill 当前状态、transition ledger、links 与
  checkpoint 一起回滚。
- `hermesgraph-reconcile-learning` 可从累计 checkpoint 重建缺失 link，检查各 artifact 的
  tenant/project/ID 与 transition job 归属，并核对 succeeded result。缺 link 自动修复；artifact
  丢失、冲突 link 或 result 不一致标为 `required`，不补写业务资产。
- 真实 Postgres contract 11/11：覆盖 artifact 后异常回滚、提交前 token 替换、Skill 状态与
  transition ledger 原子回滚、checkpoint 单调性、不可变 payload、link 修复和 artifact 丢失检测。
- Compose 镜像重建后 migration 1-9 连续，app/Postgres/Neo4j healthy、Qdrant running。首次对
  v8 历史任务 reconciliation 修复 3 条派生 link，`verified=1, required=0`。
- API worker 与临时独立 worker 再次竞争 40 个任务：40/40 均首次尝试成功并到
  `evolution_committed`。使用 Docker Postgres DSN 的全量 pytest 无跳过通过；Ruff 全绿，
  120 个应用源码 strict mypy 通过。
- 外部 Reflection provider 调用仍位于数据库事务外；模型返回到
  `reflection_completed` checkpoint 提交之间保留极小 at-least-once 窗口，因此不宣称跨 provider
  与 Postgres exactly-once。

## 2026-07-19 Skill Evolution v10 反事实回放与跨版本 Refinement

- 新增 `FrozenCapabilitySkillSandbox` 与 `counterfactual-skill-replay-v2`。候选 Skill 通过真实
  activation/execution registry 逐步消费来源轨迹的冻结 `ToolEvent`；沙箱限制总超时、最大步骤和
  fixture 输出字节数，不调用网络、数据库写工具或模型 provider。
- replay report 保存逐步 input/output SHA-256、fixture index、错误码、耗时、序列相似度和工具成功率，
  不保存工具输出原文。动作错序、fixture 耗尽/剩余、历史工具失败和步骤预算越界均失败关闭；
  `activate_skill` 控制事件不会被 miner 误学为业务步骤。
- 新增 `SkillRefiner`：仅从已有观测的父版本派生候选，证据扩展生成 patch，兼容行为扩展生成 minor，
  删除 action/capability/trigger 等 breaking change 生成 major。候选沿用 Skill ID、记录
  `parent_version`、合并来源 run 并恢复 Draft；父版本不可变，ChangeSet 保存 change level、reasons
  和 semantic diff。
- Skill repository、Evaluation/Transition repository、LearningEngine、SkillEvolutionService、
  WorkspaceService 和四个 FastAPI endpoint 全部支持可选 `skill_version`。未指定时保持旧行为取最新；
  指定后评估、健康门禁、状态迁移和历史列表始终锁定精确版本。
- migration v10 为 checkpoint/result 增加 `skill_candidate_version`，为
  `learning_job_artifact_links` 增加 `artifact_version`。reconciliation 现在验证精确 Skill 行；只保留
  同 ID 的其他版本不能掩盖候选版本丢失，旧 v9 checkpoint 仍兼容。
- 安全/回归测试覆盖原始 fixture 不泄露、动作偏差、失败工具、步骤预算、patch/minor/major、Draft
  防递归 refinement、多版本本地仓储、API 精确版本和 semantic diff。全套 pytest 203/203、Ruff、
  122 个应用源码 strict mypy 通过；真实 Postgres contract 12/12，包含同 ID 两版本与精确版本丢失。
- 新镜像已重建；app/Postgres/Neo4j healthy、Qdrant running。数据库登记
  `10:learning_artifact_link_versions`，OpenAPI 已确认 evaluate/evaluations/transition/transitions
  四类接口公开 `skill_version`。

## 2026-07-19 受控公共 Web Search

- 官方 OpenAI 文档和本机兼容端点探测共同确认新集成使用 Responses
  `tools=[{"type":"web_search"}]`。端点 `gpt-5.6-sol` 实测返回
  `web_search_call + message.url_citation`；`action.sources` 可能为空，因此实现不依赖该可选列表。
- 新增 `WebSearchRequest/WebSearchResult/WebSearchSource` 与 `WebSearchPort`，并在
  `IntegrationRuntime` 注册 `search_web@1.0.0`。能力固定为 read effect、`web:read` scope、
  45 秒默认 timeout、100 KB 输出上限和 provenance required；在该阶段根 Agent 仍只有一个
  OpenAI Agents SDK Runner，2026-07-20 已迁移为 Hermes 单循环。
- `OpenAIHostedWebSearch` 使用 `tool_choice=required`、`store=false`、有界 output tokens 和
  deployment context size/domain filters。用户 query 和网页都被声明为 untrusted；疑似 key/token
  在发网前拒绝，URL userinfo、localhost、`.local`、非 global IP、越过 allowlist 的来源在返回端拒绝。
- 每个 URL 的 citation contexts 聚合为稳定 UUID5 `EvidenceRef`，绑定当前 run ID、content hash、
  response/model/revision、query fingerprint 和 citation spans；fragment 与常见 tracking 参数清理后
  去重。原始无引用输出不返回给 Agent。
- Agent tool 有独立 `MAX_WEB_SEARCH_TOOL_CALLS`，仍同时受全局工具预算、重复输入、Capability
  timeout 和最大返回字节约束。`ToolEvent` 只记录 input hash、来源/citation 数、stop reason、usage、
  provider revision 和错误类型。
- 发布门禁补上 trust invariant：只有 verified-trust citations 才能发布 verified
  claim/confidence；模型对 untrusted Web evidence 输出 verified 时自动降为 supported，直接构造
  非法 `AnswerResponse` 会被 validator 拒绝。
- provider live gate 返回 `live_cited`：模型 `gpt-5.6-sol`、1 citation、1 public source、
  `stop_reason=cited_sources`。完整 Agent run `c5acb3aa-8146-4f1b-b92d-001907e27714` 只调用一次
  `search_web`，最终 1 个 citation 的 provenance run ID 一致，tracking 参数已清理，claim 与总体
  confidence 均为 `supported`。
- 测试覆盖 provider 凭据、domain 配置、URL citation、私网与越域拒绝、无引用丢弃、密钥查询阻断、
  client lifecycle、`web:read` scope、Agents function tool 和 publisher trust 降级。全量 pytest
  通过（保留 5 个环境型 skip），Ruff 全绿，应用与本轮测试 strict mypy 通过；最终镜像 `pip check`
  成功，app/Postgres/Neo4j healthy、Qdrant running，`/health` 与 `/v1/capabilities` 通过。

## 2026-07-19 Web Search Golden Gate v1

- 新增 `WebSearchGoldenCase/GoldenSet/Evaluator/Report`、`hermesgraph-eval-web-search` 与
  `examples/evaluation/web_search_golden.json`。13 个 case 分为 7 个 live 和 6 个 required contract，
  覆盖 freshness、一手来源、citation、domain policy、密钥/私网/提示注入、无引用、冲突、timeout/
  5xx 与中英文。
- evaluator 校验公网 URL、run-scoped `UNTRUSTED` provenance、content hash、source/citation 一致性、
  domain/primary policy、术语、观测时间和来源数量；输出 case/provider-only success、citation
  coverage、source precision、primary-source rate、term recall、freshness、policy/resilience、P50/P95、
  usage/显式价格成本及 category/difficulty/tag slices。
- `--execution contract` 使用惰性 backend；在清空环境变量、`WEB_SEARCH_MODE=disabled`、无 key 的
  进程中实跑 6/6。query 密钥阻断复用生产 pre-transport validator，确定性 timeout/5xx/无引用/私网/
  untrusted-injection fixture 不访问网络，也不进入 provider-only success。
- 报告通过 fsync + 原子替换写入，只保存 query SHA-256、公开域名、错误类型/标准码/HTTP 状态、
  usage 和指标，不保存原始 query、provider 错误正文或网页正文。401/403/404/429/5xx 与 transport
  错误分开分类，retry history 可审计。
- contract 报告 `.data/evals/web_search_contract_20260719.json` 为 6/6。当前兼容端点最小 live case
  经两次尝试均返回 `InternalServerError`/HTTP 503，报告为
  `.data/evals/web_search_live_probe_retry_20260719.json`；因此没有继续消耗其余 6 个 live case，
  也没有把历史单次 citation 成功外推为当前 provider 通过。
- 发布回归为 pytest 210/210（含真实 Postgres contract 12/12）、Ruff 全绿、124 个应用源码 strict
  mypy、Compose config 与冲突标记/尾随空白扫描通过。新镜像 `pip check` 成功，容器内 console script
  再次完成 contract 6/6；app/Postgres/Neo4j healthy、Qdrant running，`/health` 和 capability 清单正常。

## 2026-07-20 Hermes-first Runtime Migration

- 正式接受 ADR-008：`hermes-agent==0.18.2` 取代 OpenAI Agents SDK 成为唯一在线 Agent Loop；
  OpenAI Python SDK 继续承担 Responses/Vision/Structured Outputs/Embeddings，LangChain 继续承担
  Integration Runtime；OpenAI Agents SDK 在当时保留为互斥 fallback，后由 2026-07-22 ADR-009 删除。
- 新增 `HermesAgentRuntime`：启动检查 `/health`，通过 `/v1/runs` 创建任务，优先消费 SSE、失败时
  轮询，支持 timeout/cancel/stop，自动拒绝未知 approval，并且只接受 bridge 已发布答案。
- 新增 `HermesCapabilityBridge`：每个 run 创建随机 bridge ID，服务端保存 tenant/project/user scope、
  总工具预算、检索/Web 分预算、重复输入指纹、evidence allowlist 和发布状态。发布后继续调用工具、
  未知 evidence ID、重复发布或无发布终态全部失败关闭。
- 新增受信任 `hermesgraph-bridge` 插件，暴露 `search_knowledge`、`search_graph`、可选
  `search_web`、`recall_project_memory` 与 `hermesgraph_publish_answer`；原生只启用
  `memory/skills/todo`，terminal/file/browser/delegation/session search/Hermes native web 均关闭；
  terminal backend 额外强制为无 Docker socket 的 `docker`，不保留 local execution 后门。
- 稳定 Hermes 会话键使用 tenant/project/user/session 的 HMAC，不把原始作用域交给 sidecar 或模型；
  sidecar API 与内部 bridge 使用两个独立 secret，生产/预发布配置强制每个不少于 32 字符。
- Hermes 原生 `memory`/`skill_manage` 成功写入由插件 hook 镜像为 `LearningChangeSet`，状态固定
  `native_applied/requires_audit`，保留来源 run、scope、风险与回滚条件。完成 run 后到达的后台回顾
  写入仍持久化审计，但不重新进入已关闭的 SSE 事件队列。
- 新增 Hermes sidecar Dockerfile、bootstrap、持久 profile、固定配置和 `hermes_data` volume。
  官方 PyPI sdist hash 已核验，运行版本固定为 0.18.2；额外固定 `aiohttp==3.14.1` 提供 API server。
- Docker 实机验证 sidecar 返回 `platform=hermes-agent, version=0.18.2`，插件为 enabled，原生
  memory/skills/todo 与 bridge toolset 启用，非预期 toolset 关闭；五服务 Compose 启动成功，app 与
  Hermes 宿主健康端点、容器内鉴权 bridge 健康端点均返回 200，最终 profile 确认为
  `terminal.backend=docker`。
- 代码回归：全套 pytest 通过并保留 12 个环境型 skip，Ruff 全绿，126 个应用源码 strict mypy
  通过。Hermes runtime/bridge/config/API 定向合同覆盖认证、scope、预算、SSE/poll、取消、严格发布、
  fallback 与 native learning audit。
- 未完成的 live gate：宿主直接调用当前模型网关使用已配置 key 返回
  `401 invalid_api_key`；宿主与 Hermes 容器内 key 长度和指纹一致，因此不是 Compose 注入错误。
  在刷新凭据前，不把 sidecar/contract 通过表述为在线模型 E2E 已完成。

## 2026-07-21 Hermes 原生学习确定性回滚

- 新增 `deploy/hermes/plugin/native_snapshots.py`。Hermes `memory`/`skill_manage` 在
  `pre_tool_call` 阶段对目标文件或整个 Skill 目录保存精确 before snapshot，在 `post_tool_call` 阶段
  计算 after hash；staged、failed 和 no-op 不再被记为已应用学习。
- 快照固定在 `hermes_data/.hermesgraph/native_snapshots`，路径必须位于 `HERMES_HOME`，拒绝
  symlink、path traversal、特殊文件和超出 5 MB 默认预算的树。同一目标的原生写入与回滚互斥。
- 原生事件脱敏：content/file_content/old/new text 只保留长度与 SHA-256，结果只保留
  success/staged/done/error 摘要。成功写入继续追加 `native_applied/requires_audit` ChangeSet，并增加
  snapshot ID、before/after hash 与 rollback capability。
- 新增与 Hermes gateway 同进程的 native admin HTTP 服务。`8643` 不映射宿主，只接受第三份独立
  bearer token；回滚必须同时满足 manifest ready、请求 expected hash、当前目标 hash 和目标锁。
  state drift 返回 409，不覆盖后续合法学习；恢复后重新校验 before hash，Skill 同步清理 prompt cache。
- 新增 `HermesNativeLearningService` 和 scoped API：
  `GET /v1/projects/{project_id}/hermes/native-learning`、
  `POST .../{change_set_id}/review`。接受、成功回滚和失败回滚都追加 review ChangeSet，原记录不可变；
  接受与已完成回滚幂等，跨 project 返回 404，冲突返回 409，sidecar admin 不可用返回 503。
- 三个内部安全边界现在分别使用 Hermes API token、Hermes -> app bridge token、app -> Hermes native
  admin token；生产/预发布三者均要求至少 32 字符。Compose 实机验证五服务健康、插件/toolset 正常、
  app 容器访问 admin health 为 200，宿主 `8643` 无监听。
- 回归结果：227 个测试 collected，215 passed、12 个环境型 skip；Ruff 全绿；127 个应用源码 strict
  mypy 通过；Compose config 通过。新增覆盖 exact file rollback、目录 state drift、新建 Skill 撤销、
  append-only/idempotent review、失败回滚审计和 API scope。
- 模型阻塞重新定位：`55523` 监听进程是 Cockpit Tools 的 `cockpit-cliproxy`；Hermes 容器请求其
  `/models` 返回 `401 invalid_api_key`。因此本轮完成的是不依赖在线模型的原生学习治理闭环，真实
  Hermes 模型 run 仍未验收。该凭据随后确认已到期，并按用户决策延期处理。

## 2026-07-21 Hermes 原生快照生命周期 v2

- `NativeSnapshotManager` 增加全目录容量上限、80% warning/100% critical 健康状态和写前安全 GC。
  默认总容量 1 GB；超限的新写入 fail-closed，本次半快照被清理且目标锁释放。
- GC 策略按状态分离：未审阅 `ready`、`pending`、`after_hash_failed` 永不自动删除；accepted 默认
  保留 30 天，rolled-back 保留 7 天，no-change 保留 24 小时。所有期限可通过环境变量调整。
- native admin 增加 `POST /v1/native-snapshots/{id}/accepted` 与内部 `POST .../gc`；accepted 使用
  `after_hash` 乐观前置条件并幂等返回固定 `retention_until`。GC 不暴露为公开 FastAPI 写接口。
- 应用审阅接受时登记生命周期并把截止时间写入 append-only review ChangeSet。admin 暂时不可用时
  仍可接受，但 manifest 不标记 accepted，GC 因而保守不删除；审阅 detail 记录登记延期。
- 新增公开只读 `GET /v1/hermes/native-learning/health`，返回容量、状态计数、GC 候选数与备份元数据，
  不暴露正文、绝对路径、snapshot hash 或 token。
- 审计 Hermes 0.18.2 full backup 实现确认 `.hermesgraph/native_snapshots` 不在排除目录中，会随完整
  `HERMES_HOME` 备份进入归档；独立恢复演练仍作为下一阶段工作，文档未把“包含”误报为“可恢复”。
- 回归为 236 collected、223 passed、13 个环境型 skip；socket 路由测试因当前沙箱禁止 loopback
  bind 而跳过，最终 Docker 内网实测已补齐：裸 token 为 401、严格 Bearer 为 200、GC dry-run 为 200
  且删除 0。Ruff 全绿、127 个应用源码 strict mypy、Compose config 均通过；最终五服务运行，
  app/Hermes/Postgres/Neo4j healthy，Qdrant running。

## 2026-07-22 删除 OpenAI Agents SDK Fallback

- 接受 ADR-009。Hermes Agent 0.18.2 继续作为唯一在线 Agent Loop；OpenAI Python SDK 只负责
  Responses、Structured Outputs、Vision、Embeddings、hosted Web Search、图谱抽取和结构化反思。
- 删除 `app/agent/openai_runtime.py`、Agents SDK session/compaction adapter、fallback 专用测试和
  tracing/session 配置。`RUNTIME_MODE` 现在只接受 `hermes|offline`，传入 `openai` 直接配置失败。
- bootstrap 不再存在第三运行时分支；内置架构知识图的 runtime 节点从 OpenAI Agents SDK 改为
  Hermes Agent，避免 Agent 从旧种子知识中学回已废弃架构。
- `pyproject.toml` 改为显式依赖官方 `openai`，两个 lock 删除 `openai-agents` 及其独占传递依赖
  `mcp/griffelib/httpx-sse/PyJWT/sse-starlette`。LangSmith 仍需要的 `requests/websockets` 保留。
- OpenAI-compatible provider 仍由 `AsyncOpenAI(base_url=...)` 支持；这次删除不影响本地兼容模型、
  DeepSeek 类端点、检索 planner、Vision、embedding 或图谱抽取。
- 当前代码回归为 228 collected、215 passed、13 个环境型 skip；Ruff 全绿，125 个应用源码 strict
  mypy、Compose config 均通过。干净 app 镜像 `pip check` 通过，容器探测为
  `agents_module=false`、`openai_agents_distribution=false`、`openai_version=2.45.0`；最终五服务运行，
  app/Hermes/Postgres/Neo4j healthy，Qdrant running。

## 2026-07-29 前端工作台重构

- 参考 Linear、Microsoft 365 Copilot、IBM Carbon 和 GitHub Primer 的当前实践，重新定义为
  高密度、低噪声的个人 Agent 工作台：统一页面标题、导航、工具栏、正文层级与语义状态色。
- 原十个平铺导航入口重组为“工作 / 知识 / 系统”三组；个人设置独立放在侧栏底部。移动端固定
  四个高频入口，其余页面进入“更多”面板，不再横向滚动十个入口。
- 新增全局命令面板，支持页面检索、新建对话、刷新工作区与完整键盘选择；顶部栏只保留全局上下文、
  命令入口、学习状态和工作区级操作。
- 重做 Agent 对话首屏：输入区进入任务起点，直接展示文档、长期记忆、活跃技能和学习变更状态；
  消息区补充身份标记、回答复制、证据与反馈操作，运行 Inspector 保持独立证据面板。
- 行动中心增加待处理、进行中、受阻、已完成概览；修复筛选列表为空时仍显示旧任务详情的状态错误。
  全局正文提升到可读字号，表格与表单保持紧凑但不再依赖 8–10px 文本。
- 生产构建通过。浏览器在 1440x900 与 390x844 下逐页检查全部十个视图，`body`、根节点与主数据面
  横向溢出均为 0；命令面板、移动端“更多”、聊天、行动、图谱和个人设置完成截图复核，应用控制台
  无错误。

## 2026-07-29 普通对话与证据模式分流

- 复现“你好”返回 `No evidence was found for this task.`：`8011` 当时运行的是临时 offline
  uvicorn，使用空本地仓库和 `OfflineAgentRuntime`；真实 Hermes/Postgres/Qdrant/Neo4j 服务位于
  `8001`。已停止错误预览进程，并让 `8011` Vite 开发服务代理真实 `8001` 后端。
- 新增 `AnswerMode` 合同：`grounded` 用于事实、研究和知识检索回答，继续执行 claim/citation/
  confidence 证据门禁；`conversational` 用于问候、感谢、澄清和不含外部事实的社交对话；
  `action` 用于已确认工具行动的结果回执。前端只在 grounded 模式显示证据置信度，普通聊天不再显示
  红色 `INSUFFICIENT`。
- Hermes prompt 与发布工具 schema 强制每次选择回答模式。非 grounded 草稿在
  `AnswerPublisher` 中确定性清空模型误填的 claim/citation，并把证据 confidence 归一为
  `insufficient`；证据规则仍只在 grounded 模式生效，不能用 conversational/action 绕过事实检索。
- 真实 UI 点击暴露兼容网关偶发 `HTTP 502 ... upstream_error ... EOF`。Hermes 0.18.2 原分类器会
  因外层 `invalid_request_error` 把它误判为不可重试格式错误。Hermes 镜像现在执行版本锁定、锚点
  校验的最小补丁：对 `502` 中明确的 upstream transport 信号优先分类为 retryable
  `server_error`，复用 Hermes 原生三次退避重试；重试发生在模型首响应阶段、任何工具执行之前。
- 修复侧栏在线状态：`runtime_mode=hermes` 现在显示“Hermes 在线”，不再错误显示“离线模式”。
- 验证结果：运行容器内同型 `502` 分类为 `server_error True`；真实页面发送“你好”返回正常中文
  `conversational` 回复，无检索、claim、citation 或置信度徽标。前端 production build、Ruff、
  Hermes 定向合同和全套 pytest 均通过；全套仅保留原有环境型 skip。
- 新增 `ConversationRoutedRuntime` 三层分流。完整短社交消息走无模型确定性响应；通用领域的普通
  闲聊走只暴露 `delegate_to_agent` 的一次轻量模型调用；研究/技术领域、事实或时效问题、知识库/
  图谱/文件/个人记忆、引用和行动请求升级到 Hermes。直接通道异常时 fail-safe 升级，不直接生成
  无依据事实。
- 纯 conversational 完成事件不再自动提交 reflection/mining，避免问候污染 Experience Bank；
  用户显式反馈仍进入学习闭环。SSE 状态从无条件“规划和检索”改为中性的“正在理解请求”，终态按
  conversational/grounded 分别显示整理回复或整理有依据的回答。
- bridge 新增 publication event。`hermesgraph_publish_answer` 一旦成功，应用立即停止 Hermes run
  并返回已通过证据门禁的 artifact，不再等待 sidecar 的发布后模型回合。真实专业请求完成
  `search_knowledge -> 10 evidence -> 6 claims/4 citations -> publish`，此前 90 秒超时已消除。
- 部署后实测：`你好` 的非流式接口 79 ms、完整前端 SSE 641 ms，零工具、零学习变更；情绪闲聊
  “我今天有点累，陪我聊两句”用时 4.89 秒，零工具、零学习变更；通用领域的专业知识+引用请求由
  轻量路由在 4.98 秒判定为 `agent`。真实研究检索完整返回约 80 秒，耗时来自 gpt-5.6 推理和
  agentic retrieval，不再影响普通聊天。

## 2026-07-30 会话路由 v2 与量化 Skill 晋级

- 快速通道不再把“社交判断”等同于正则匹配。只有完整问候、感谢、告别和无历史确认词走
  deterministic；开放式闲聊继续调用模型。`TrajectoryConversationHistory` 按
  tenant/project/user/session 读取最近已完成的 8 轮、最多 12,000 字符，排除当前 running run；
  有历史的“好的/继续”交给模型理解，历史和当前消息都保持 untrusted。
- 快速模型与主 Agent 模型分离：`gpt-5.6-luna` 只拥有 `delegate_to_agent`，Hermes 继续使用
  `gpt-5.6-sol`。事实、时效、专业知识、图谱、知识库、文件、记忆、引用、行动和高风险问题必须
  升级；模型异常、空响应和历史读取异常同样 fail-safe。每个新 run 固化
  `routing_lane` 与 `component_versions.conversation_router=2`，旧运行单列 `legacy`。
- 新增 22-case 中英文路由黄金集、评测器与 `hermesgraph-eval-conversation-routing`。容器 live
  报告 `.data/evals/conversation_routing_20260730.json` 为 22/22、危险直答 0、过度升级 0、
  mean 2.220 s、P95 4.024 s；混淆矩阵为 deterministic 4/4、conversation 6/6、agent 12/12。
  真实 API 验证完整问候走 deterministic，两轮情绪对话连续走 conversation 并正确使用上一轮语境。
- Skill 健康计算新增不可变 `SkillPromotionEvidence`：冻结 scope/version、窗口、原始/有效观测、
  run、evaluator、指标、阈值和建议动作。同一 run 的显式反馈覆盖旧 run outcome 进入有效窗口但
  不删除审计记录；轻度负反馈生成幂等 rollback recommendation，严重负反馈或窗口退化确定性回滚。
  Canary/Active 晋级继续要求人工批准，自动学习不能扩大流量。
- 现有 `learned_what@0.1.0` 先用当前 `counterfactual-skill-replay-v2` 重新通过 3/3 frozen
  capability replay，再依据 3/3 Shadow promotion evidence 和用户授权晋级到 Canary。当前 Canary
  健康为 0/5、建议 `hold`；没有伪造在线激活样本，也没有越过门禁直接设为 Active。
- 本轮明确未执行论文入库或 KG backfill。代码回归为 291 collected、278 passed、13 个环境型 skip；
  Ruff 全绿、148 个应用源码 strict mypy、前端 production build、Compose config 和镜像
  `pip check` 均通过。最终 app/Hermes/Postgres/Neo4j healthy，Qdrant running。

## 2026-07-30 开源差距审计、MemoHarness 落地与 Hermes 0.19

- 对照 Microsoft GraphRAG、LightRAG、KAG、RAGFlow、LlamaIndex、Haystack、Graphiti、Mem0、
  Letta 和 Hermes 官方仓库，新增 `docs/OPEN_SOURCE_AGENTIC_RAG_GAP_ANALYSIS.md`。不引入第二
  Agent Runtime；优先差距锁定为 Pattern 晋级/consumer、reranker/entailment、temporal fact 和
  GraphRAG community/global/DRIFT。
- 落地 `app/harness/`：typed D1-D6、CaseFeatures、deterministic diagnosis、不可变
  Experience/Evaluation、E+/E-、Postgres v12、durable checkpoint/reconciliation/backfill。
  生产回填得到 33 Experience、33 Evaluation、8 个 negative；二次运行新增 0、冲突 0。
- 落地 Postgres v13 Pattern/Overlay：确定性 Draft miner、支持/反例链接、稳定身份与版本、
  run-scoped observe/shadow selector、冲突 no-op、overlay identity/hash/pattern version 冻结和
  scoped API。真实 33 条样本未达到 cluster/repeat/contradiction 门槛，因此保守生成 0 Pattern。
- Hermes 升级到官方 `0.19.0`，固定 wheel SHA-256
  `bd0bac012aee38a60894781f4597dc29ee7bedb3448540249921f10d3bef327f`。显式注入 scope-bound
  conversation history；首个发布后立即返回用户，但不再调用 `/stop`，让 Hermes 正常 finalizer。
- `hermesgraph_publish_answer` 改为 exactly-once artifact + 幂等重复回执：模型重复调用不会覆盖首个
  答案、不会新增发布事件，发布后其他业务工具继续拒绝。
- 每个隔离 Agent 回合显式设置 Memory/Skill review cadence 为 1。真实回合首发约 12 秒返回，
  Hermes 随后以 `text_response(finish_reason=stop)` 完成；后台 `bg-review` 调用了 `memory`、
  `skills_list` 和 `skill_manage`，生成了合理的用户约束偏好与通用
  `constraint-following-responses` Skill。
- 后台 review 使用父 `session_id` 回连原 run，只有 Hermes 0.19 的 `bg-review` 线程可以发送
  completion 信号；App 等待该信号或有界超时后才释放 bridge。最终修正版已部署并在容器内确认
  thread guard、parent-session mapping 与 cadence，纵向模型重验仍被 provider `429 model_cooldown`
  阻断。
- Experience evaluation 在 JSON 与 PostgreSQL 都固定按
  `created_at -> run_outcome -> explicit_feedback -> evaluation_id` 排序，消除同时间戳反馈导致的
  学习证据顺序漂移；15/15 真实 PostgreSQL adapter contract 通过。
- 最终回归为 310 collected、294 passed、16 个环境型 skip；Ruff 全绿，159 个应用源码 strict
  mypy 通过。App、Hermes、Postgres、Neo4j healthy，Qdrant running。
- 修复后台 fork 审计关联：native tool hook 使用父 `session_id` 而不是临时 `task_id`；增加
  `on_session_end` completion event，应用在 completion 或 180 秒 timeout 前保留 bridge，避免迟到
  Memory/Skill ChangeSet 404。主回合、重复发布、父 session 关联和延迟释放均有无模型合同测试。
- 最终 live 重验时上游返回 `429 model_cooldown`，不是代码异常。主回合和原生 review 的前一轮
  live 证据保留；completion 握手当前由 unit/bridge contract 验证，provider 恢复后补一次 live。

## 2026-07-30 MH-011 Pattern 治理与 MH-014 bounded consumer

- 新增 `HarnessPatternEvaluation`、`HarnessPatternPromotionEvidence` 和
  `HarnessPatternTransition`。Evaluation 记录 supporting/contradicting/required case 的数值投影，
  Promotion Evidence 冻结 dataset revision、阈值、evaluation hash 和晋级结论，Transition 只引用
  exact evidence hash；三者同 ID 异 hash 均拒绝。
- 状态机固定为 `Draft -> OfflinePass -> Shadow -> Canary -> Active -> Deprecated`。Draft 到
  Shadow 在 required cases、scope/hash、evidence integrity 和 non-regression 全通过后自动顺序
  晋级；Canary/Active 必须人工批准，跳级和 stale expected status 均拒绝。
- PostgreSQL migration `14:harness_pattern_governance` 新增 Evaluation、Promotion Evidence 和
  Transition 三张表。应用 transition 在 pattern 行锁内校验有效状态，并使用 applied-from 唯一
  索引阻止并发双晋级；真实 16/16 adapter contract 通过。
- `RunExecutionPolicy` 在 run start 解析、clamp、hash，并同时冻结到 `RunContext` 和
  `RunSnapshot`。Observe/Shadow 永远 `behavior_applied=false`；Canary 按
  revision/scope/run/pattern-version 稳定分桶；bank/ledger/projection 异常 fail closed 到 baseline。
- 第一版只开放 `capsule_memory_limit`、`memory_min_confidence`、`retrieval_profile`、
  `max_subqueries`、`max_retrieval_rounds` 和 `graph_hops`。Capsule、Agentic Retrieval Controller、
  IntegrationRuntime 和 Hermes Bridge 只读取 run-local frozen policy，不修改全局实例，也不能提高
  工具预算、timeout、scope 或 publisher 权限。
- 并发测试确认同一个 Retrieval Controller 可同时处理 baseline 与 1-subquery/1-round policy，
  不发生配置串扰；IntegrationRuntime 与 Bridge 对 graph hop 做双层 clamp，fingerprint 使用规范化
  payload。
- 部署后 PostgreSQL v14 和三表均存在，App/Hermes/Postgres/Neo4j healthy，Qdrant running。
  真实问候 run `960d833f-e7e1-47e7-b0ae-aee73456a600` 约 45 ms、零工具，Observe policy 完整冻结
  且 `behavior_applied=false`。
- 最终回归为 322 collected、305 passed、17 个环境型 skip；Ruff 全绿，162 个应用源码 strict
  mypy 通过。生产 Experience Bank 仍只有 33 条且 miner 为 0 Draft，没有伪造 Pattern 晋级数据。

## 下一检查点

1. 为公开 `/v1` 控制面增加可配置 bearer/session auth、tenant/project/user scope binding 和
   destructive/review action authorization；本地开发可显式选择单用户模式，生产不能默认匿名。
2. provider 冷却恢复后重跑 Hermes 0.19 background review completion 纵向门禁，要求所有 native
   audit 与 completion callback 为 200，ChangeSet 可接受/回滚。
3. 在独立临时 `hermes_data` volume 执行 full backup -> restore -> manifest/hash/rollback 校验，并
   补 Hermes 版本升级、容量耗尽、worker 强杀与恢复矩阵。
4. 恢复 RAG 开发时按差距文档顺序实施 reranker/entailment、temporal fact、community/global/DRIFT；
   KG backfill 继续作为独立数据维护，不与这些架构工作混合。
5. `MH-015` Pattern Canary health/auto rollback 延后，不再优先于用户可直接感知的交互闭环。

## 2026-07-31 可恢复会话与显式记忆交互

- 修复工作台把所有聊天固定到 `session_id=workbench` 的问题。“新建对话”现在生成独立 session，
  前端发送请求时携带当前 session，不再出现清空界面但后端继续读取旧上下文的假新对话。
- 新增 scoped 会话列表和会话运行恢复 API。会话标题来自首轮输入，预览来自最新回答，历史按时间
  正序恢复；tenant/project/user/session 隔离合同覆盖同名 session 的跨用户数据不可见。
- 当前 session ID 和未发送草稿保存在本机；已发送消息全部从 trajectory 恢复。真实浏览器验证完成
  “发送你好 -> 新建空会话 -> 切回原会话 -> 刷新页面仍恢复问答”。草稿按 session 惰性初始化，
  不会在组件挂载时被空值清理误删。
- 新增显式长期记忆 API 和消息级书签操作。记录使用稳定内容 hash、`user_asserted` provenance、
  1.0 confidence 和 session 来源；重复点击返回同一 memory ID，其他 user 不可见。
- 点踩现在可附带改进说明；失败回答提供重新发送。SSE provider 错误归一为稳定 code/message/
  retryable，不再把包含网关内部详情的原始异常显示在聊天中。
- 会话选择器修正 `research/software_docs` 中文标签，并加入更新时间与轮数以区分同名历史会话。
- 真实模型上下文纵向测试在第一轮被 provider `429 model_cooldown` 阻断，未宣称 live 通过；本地
  会话恢复、`TrajectoryConversationHistory` scope 合同和 API 纵向测试通过。
- 分块器会自动发现仓库或 Docker 内置的 `o200k_base` tiktoken 缓存，宿主离线启动和全量测试不再
  尝试访问公共 blob。最终 324 collected、307 passed、17 个环境型 skip，Ruff、strict mypy 和
  frontend production build 全部通过。
- 390 x 844 移动端实测无横向溢出或越界按钮；会话选择器、消息操作、反馈入口和输入框均可见。

## 2026-07-31 会话管理与聊天附件

- 新增 `ConversationMetadata` 与 scoped JSONL repository。会话自定义标题和归档状态持久化在
  `/data/conversations.jsonl`，按 tenant/project/user/session 隔离；归档不会删除 trajectory。
- `GET conversations` 支持 `include_archived`，`PATCH conversations/{session_id}` 支持重命名、
  归档和恢复。API 测试覆盖跨用户同名 session 不串扰、默认隐藏归档、显式返回和恢复。
- 工作台增加对话管理菜单、内联重命名和归档恢复列表。真实浏览器完成“新建问候 -> 重命名 ->
  归档切到空会话 -> 从归档列表恢复”的完整操作。
- 聊天输入框增加回形针和拖放入口，最多 5 个附件；同步模式直接入库，异步模式提交 durable job
  并轮询上传/解析/成功/失败状态。未成功附件会阻止发送，避免检索尚未完成时开始回答。
- 附件名随运行输入保存为末尾结构化块，前端恢复时解析为文件标签；会话标题和预览只使用用户可见
  文本，不泄露内部 `<attachments>` 标记。
- 真实 TXT 文件通过聊天入口完成 durable ingestion，知识文档数从 4 增至 5、附件变为“可提问”，
  随消息显示并可停止当前任务；验收文件和两条测试会话随后逻辑归档，不污染活跃工作区。
- 桌面和 390 x 844 移动端均无横向溢出或越界按钮；移动端管理菜单边界为 89-379 px。

## 2026-08-01 首次设置、会话搜索与运行反馈

- 新增可跳过的首次 Persona 设置，采集用户称呼、当前角色/关注方向、语气和兴趣，直接写入现有
  versioned Persona；跳过只产生 24 小时本地展示冷却，不创建另一套用户档案，也不阻塞聊天。
- 命令面板扩展为页面、操作和历史会话统一搜索。会话按标题、最新预览和更新时间匹配，选择后通过
  现有 scoped session API 恢复真实 trajectory；空查询仍保持常用命令，不让历史记录淹没操作入口。
- SSE `run.heartbeat` 的 `elapsed_ms` 已接入运行状态；完成消息显示真实耗时，并仅在存在时补充工具
  调用数和学习更新数。普通“你好”实测 633 ms 返回，无 RAG 工具调用、无横向溢出。
- 首次引导在 1280 x 720 与 390 x 844 均完成真实浏览器验收，移动端按钮、表单和滚动边界无越界；
  历史会话搜索、打开和状态恢复纵向通过。验收产生的临时会话已归档，Persona 已恢复未完成状态。
- 最终基线保持 324 collected、307 passed、17 个环境型 skip；Ruff 全绿、162 个应用源码 strict
  mypy 通过，前端 production build 和 Docker 部署通过。

## 2026-08-01 长任务活动时间线与停止反馈

- stream API 在运行前预分配 `run_id`，订阅同 ID 的进程内 `RunEventRecorder`。工具完成事件同时复制
  到 SSE queue 和 trajectory buffer，前台实时展示不会消费审计数据；结束、异常和取消均解除订阅。
- 工作台消息内新增可展开活动时间线，将工具映射为知识库检索、知识图谱查询、公开网页搜索、工作区
  读取、图片分析、长期记忆、任务/计划/笔记、技能检查和回答发布。未知能力只显示“受控工具”，不
  暴露原始工具名、参数、output summary 或模型思维。
- `run.error` 保留稳定 code、retryable、阶段和 duration。provider busy 实测持续显示连接与耗时，
  21 秒后收敛为“任务未完成/重试任务”；主动停止约 1.4 秒收敛为“任务已停止/重新开始”。
- 临时离线真实运行纵向捕获到 `请求已接收 -> 检索知识库 -> 回答已生成`，`tool.completed` 在回答
  delta 前进入 UI；完成摘要为 `1.3 s · 1 个工具`。临时实例随后关闭，验收会话均已归档。
- 1280 x 720 与 390 x 844 均无横向溢出，移动端时间线宽 362 px、停止后的操作按钮未越界。全量
  基线为 326 collected、309 passed、17 个环境型 skip；Ruff、strict mypy、production build、镜像
  `pip check` 和五服务健康检查通过。

## 2026-08-01 聊天快速记录与个人事务闭环

- 新增聊天内 `QuickCapture`，任务、日程和笔记不经过模型即可写入现有 Personal API。日程固定映射为
  带 `due_at` 与 `scheduled` 标签的 Task，避免并行维护 Event 与 Task 两套事实；笔记写为指定日期的
  daily Note。
- 保存结果保留服务端 record ID/日期并提供“查看”：任务或日程切到行动中心且精确选中记录，笔记切到
  对应日期的日历回顾。跨视图 focus 只负责导航，页面仍重新读取 scoped API，不把前端状态当事实源。
- 日历日期格新增到期任务数量；选中日期可查看并完成当天安排、查看或新增 daily Note。`seal_day`
  同步纳入当天笔记，归档 summary、diary 和 highlights 可以反映用户当天留下的记录。
- 单元纵向测试扩展到 daily Note 归档聚合；全量 326 collected、309 passed、17 个环境型 skip，Ruff、
  162 个应用源码 strict mypy 和 frontend production build 全绿，Docker app 镜像已重建。
- 浏览器使用端口 8002、独立 `/tmp/hermesgraph-personal-ui` 数据目录完成“创建日程 -> 精确查看 ->
  完成当天安排 -> 创建笔记 -> 生成归档”的真实纵向测试；生产 8001 未写入验收记录。1280 x 720 与
  390 x 844 均无横向溢出，临时服务验收后已关闭。

## 2026-08-01 持久 Run Event 与刷新续传

- 将运行所有权从单个 HTTP generator 移入 `RunStreamCoordinator`。`POST runs/start` 使用客户端
  idempotency key 先持久化 running trajectory 并返回稳定 run ID；重复请求返回同一 run，模型与工具
  不会执行第二次。旧 `POST runs/stream` 继续兼容，但内部复用同一 coordinator。
- `RunEventRecorder` 新增追加式 `run_events.jsonl`：每个 run 的 event cursor 从 1 单调递增，支持
  scoped backlog、无缝注册订阅、实时 fan-out 和进程重载后读取。SSE 写出标准 `id`，GET stream 通过
  `after_cursor` 只续传未确认事件。
- 浏览器断开只 unsubscribe，不再 cancel 服务端 task。工作台刷新后从 session trajectory 识别 running
  run，重建用户消息、活动状态、工具和回答增量；连接抖动自动从最近 cursor 重连。停止按钮改为
  `DELETE runs/{run_id}`，服务端持久化 cancelled trajectory 与 `run.cancelled` 后前端才显示已停止。
- App 进程重启无法恢复任意在途模型 coroutine；没有 owner 的旧 running trajectory 会确定性转为
  failed、标记 `run_interrupted` 并产生可重试终态，避免永久假运行。这一边界明确保留，不为当前
  单机产品引入复杂分布式工作流。
- 新增持久事件 scope/cursor、观察断开、幂等 coalesce、显式取消和 API resume 合同。全量为
  330 collected、313 passed、17 个环境型 skip；Ruff、163 个应用源码 strict mypy、frontend
  production build 和镜像 `pip check` 全绿。
- 隔离端口 8002 使用 10 秒 SlowRuntime 完成真实浏览器纵向验收：第一轮运行中刷新后继续同一 run
  并完成；第二轮刷新后显式停止并保存 cancelled。两次输入正好产生 2 条 trajectory，事件日志 37 条；
  390 x 844 下 `scrollWidth=390`。隔离服务与数据已清理，正式 8001 镜像已部署且五服务健康。

## 2026-08-02 本地任务提醒与通知中心

- 新增确定性 `TaskReminderFeed`：只读取开放 Task 的 `due_at` 和 Persona IANA timezone，投影为
  `overdue / due_soon / today`，不创建第二套 Event/Reminder 事实。完成或归档任务自动退出提醒流。
- 新增 `TaskReminderState`，只保存当前 `task_id + due_at + kind` 的 `read_at` 与
  `snoozed_until`。任务改期、由 today 进入 due-soon、再进入 overdue 都会重新成为未读；延后中的
  同一到期版本暂时隐藏。
- Personal API 增加提醒列表、单条已读、全部已读和延后；JSON repository 直接兼容，Postgres 使用
  全局 migration `15:personal_reminder_state`。首次误用已被 MemoHarness 占用的 v12 时，真实启动
  门禁拒绝重复版本；随后加入不依赖数据库环境的全局迁移唯一/连续测试，生产 v15 已实际应用。
- 工作台顶部加入通知中心，支持未读角标、任务跳转、已读和延后 1 小时；每 60 秒以及页面恢复可见时
  轻量刷新。浏览器系统通知只在用户主动点击授权后启用，并按 task/due/kind 去重。
- 生产 8001 真实纵向完成“创建即将到期任务 -> 未读 1 -> 已读 0 -> 精确打开行动中心 -> 延后为空 ->
  改期后重新未读 -> 归档清理”。1280 桌面布局清晰；390 x 844 的通知面板边界 10–380 px，页面与
  面板 `scrollWidth` 均等于可视宽度，console 无 error。验收任务已归档，工作台恢复 0 条提醒。
- 最终全量为 332 collected、315 passed、17 个环境型 skip；Ruff 全绿、163 个应用源码 strict mypy、
  React/TypeScript production build、镜像 `pip check` 与五服务健康检查通过。

## 2026-08-03 研发团队业务主线与体验交付基线

- 产品默认业务主线调整为研发团队 Engineering Intelligence Agent，覆盖内部架构、服务、API、ADR、
  事故、Runbook、团队归属、工程入职和技术调研。个人学习保留为 `workspace_mode=personal`，只改变
  默认内容、来源和页面术语，不创建第二个 Agent Loop、第二套存储或第二套学习治理。
- 新增 `docs/ENGINEERING_INTELLIGENCE_AGENT_DELIVERY.md`，作为交给 Luner 的实施主文档。它固定
  六个任务入口、对话/证据/知识/系统地图/学习体验、软件工程 DomainPack、来源分层、时效关系、
  Answer contract、fixture importer、三层评测、性能预算、七阶段门禁和最终 Definition of Done。
- 当前优先级固定为体验 P0。GitHub、飞书、Jira、Notion、Confluence 等连接器、多组织管理和自动写
  代码/发布全部延期；界面不得提前展示空壳入口。现有 528 篇 arXiv 论文不删除，但隔离为个人公共
  参考层，不参与默认企业工作区和首轮企业验收。
- 新增完全虚构的 Northstar Labs / Atlas 企业研发语料：manifest 管理 23 份文档，覆盖 7 个服务、
  架构与请求链路、OpenAPI、团队归属、4 个 ADR（含 superseded 冲突）、2 个事故、2 个 Runbook、
  入职、发布说明、SLO 和 1 个不可信工单安全样本。语料不含真实公司、凭据或客户数据。
- 新增 10-case 答案级黄金题集，覆盖架构多跳、负责人和依赖、事故因果、现行/历史 ADR 冲突、
  有序 Runbook、服务职责比较、入职计划、JWKS 影响分析、不存在组件和提示注入。每个 case 声明
  required/forbidden sources、required facts、forbidden claims、图路径和引用下限，后续编译为
  Retrieval、Graph、Answer 三层门禁。
- 本检查点只完成产品与交付设计、测试语料和静态验收合同，未把 23 份 fixture 导入正在运行的
  Postgres/Qdrant/Neo4j，也未宣称新前端体验已经实现。实现必须按交付文档逐阶段完成并回写真实结果。

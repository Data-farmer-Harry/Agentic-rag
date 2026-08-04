# Open-source Agentic RAG Gap Analysis

最后更新：2026-07-31

## 1. 结论

HermesGraph 已经是一个有界、证据优先的 Agentic RAG，而不是普通的固定链 RAG。它具备唯一 Hermes
Agent Loop、动态工具选择、向量/关键词/图谱联合检索、有界二次补检、证据白名单发布、个人 Memory/
Skill、durable learning 和 observe/shadow 经验层。

当前最值得补的不是再引入一个 Agent 框架，而是四类能力：

1. GraphRAG community/global/DRIFT 检索；
2. Graphiti 风格的时态事实、失效和 supersession；
3. cross-encoder/LLM reranker 与 claim-evidence entailment；
4. MemoHarness Pattern 的 Canary health、自动回滚和 Memory/Skill/Policy fixation router。

Hermes 继续是唯一在线 Agent Runtime。LlamaIndex、Haystack、RAGFlow、KAG、LightRAG、Mem0、
Letta 和 Graphiti 只作为能力设计参考，不并入第二个控制循环。

## 2. 对照项目

| 项目 | 值得借鉴的核心能力 | HermesGraph 当前覆盖 | 结论 |
| --- | --- | --- | --- |
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | Entity/Relation、community、community report、Local/Global/DRIFT Search | 有证据图谱、多跳、实体解析、向量图融合；无 community/global/DRIFT | 采用其查询范式，不引入整套索引 runtime |
| [LightRAG](https://github.com/HKUDS/LightRAG) | local/global/hybrid/mix 模式、轻量图检索、rerank | 已有有界 hybrid + graph tools；无显式 global mode 和学习型 reranker | 增加模式路由与 reranker |
| [KAG](https://github.com/OpenSPG/KAG) | schema constrained/free、logical-form reasoning、知识对齐 | 有 typed graph 和 allowlisted traversal；无逻辑形式分解 | 复杂关系问答阶段引入 typed query IR |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 文档解析、混合检索、rerank、连接器、工作流 | Document IR、OCR、chunk、Qdrant、durable ingestion 已覆盖；连接器和可视工作流较少 | 不换框架，补连接器和 reranker |
| [LlamaIndex](https://github.com/run-llama/llama_index) | Workflows、agentic retrieval、query engine 组合 | Hermes + LangChain Integration Runtime 已覆盖组合；在线 run 不可恢复 | 长任务只借鉴 workflow event/checkpoint |
| [Haystack](https://github.com/deepset-ai/haystack) | typed pipeline、条件分支、snapshot/resume | LangChain LCEL + Postgres job 已有；交互 run 缺 cursor/resume | 增加 durable Run event，不建立第二 Agent |
| [Graphiti](https://github.com/getzep/graphiti) | episode、valid_at/invalid_at、fact supersession、时态混合检索 | 来源/revision/archive 已有；事实级时态语义不足 | P1 引入 temporal fact ledger |
| [Mem0](https://github.com/mem0ai/mem0) | user/agent/session scope、memory extraction/update/delete、graph memory | 作用域、纠错、撤回、Hermes Memory、项目 Memory 已有 | 重点补时态冲突与 memory fusion |
| [Letta](https://github.com/letta-ai/letta) | stateful memory block、自编辑记忆、Skill | Hermes 原生 Memory/Skill 与治理 Skill 双通道已覆盖 | 保持 Hermes ownership，不引入第二 runtime |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | 原生 Memory/Skill/Todo、后台自改进、profile、gateway | 已作为唯一 runtime，并升级到 0.19.0 | 继续优先使用原生能力，桥接业务证据与治理 |

## 3. 当前能力事实

### 已完成

- Hermes Agent `0.19.0` sidecar 是唯一在线 Agent Loop。
- 通用聊天先由会话路由判断；只有需要复杂推理、知识、图谱、文件、个人能力或行动的请求进入 Hermes。
- Hermes 可按任务选择 knowledge、graph、web、workspace、project memory、personal 和 governed Skill
  工具；进入 Agent lane 不等于强制 RAG。
- 最终答案必须经过 `hermesgraph_publish_answer`；首个发布结果不可变，重复发布幂等忽略。
- 用户可在首个已验证发布后立即收到答案；Hermes 主 run 在后台正常收尾。
- 同 scope 的历史对话被显式注入 Hermes `conversation_history`。
- 每个隔离 Agent 回合都触发 Hermes 原生 Memory/Skill 后台 review。review fork 使用父
  `session_id` 关联 bridge；Memory/Skill 写入有写前快照、脱敏 ChangeSet、接受和条件回滚。
- 后台 review 完成通过 `on_session_end` 握手；应用在握手或超时前保留 bridge，避免迟到审计 404。
- Experience Bank 已实现并回填：33 条 Experience、33 条 Evaluation，重复回填 0 新增、0 冲突。
- Pattern Bank/Postgres v13、确定性 miner、E+/E- selector、run-scoped observe/shadow overlay 和
  overlay identity/hash 已实现。真实样本未达到稳定阈值，因此 0 Draft Pattern 是正确的保守结果。
- Postgres v14 Pattern Evaluation、Promotion Evidence、append-only Transition、required-case
  hard gate、人工 Canary/Active、稳定分桶和 bounded `RunExecutionPolicy` 已实现。
- Capsule memory limit/confidence、retrieval profile/subquery/round 和 graph hop cap 已在 run-local
  消费；Observe/Shadow 保持 `behavior_applied=false`。

### 部分完成

- GraphRAG 有局部实体、多跳路径、冲突和向量图融合，但无 community report、global、DRIFT。
- 自进化有 Hermes 原生直接学习与 HermesGraph 治理学习；批准后的 Pattern 已能影响低风险参数，
  但生产 Pattern Bank 当前仍为 0 Draft，且 Canary health/auto rollback 尚未完成。
- Qdrant hybrid 已上线，但 production embedding 和学习型 reranker 未完成。
- Neo4j 结构图完整，当前 active 语义实体/关系受 KG backfill 进度限制。
- durable learning job 可恢复；在线交互 run 仍缺持久 event cursor/resume。

### 未完成

- Pattern Canary health、负反馈覆盖、退化聚合和自动 rollback。
- Memory/Skill/Policy fixation router 与 Hermes native ownership-conflict audit。
- 时态 fact ledger、validity interval、supersession-aware retrieval。
- community detection/report、global/DRIFT query mode。
- cross-encoder/LLM reranker、claim-evidence entailment。
- 生产终端用户认证和 destructive action authorization。

## 4. Hermes 自学习保证

“保证自学习”在本项目中定义为可验证的系统合同，而不是保证模型永远学对：

1. 每个进入 Hermes 的 Agent 回合都运行原生 Memory/Skill review；
2. review 与用户响应解耦，不增加前台等待；
3. review 只能调用 Hermes 原生 memory/skill 工具；
4. 写入前保存 bounded snapshot，写入后计算 hash 并生成脱敏审计；
5. review fork 的工具事件必须关联原 run，完成后显式释放 bridge；
6. 错误学习可接受、拒绝或在 after-hash 未漂移时确定性回滚；
7. HermesGraph Pattern 只有通过 Draft -> Shadow -> Canary -> Active 才能改变后续行为；
8. 任何学习都不能扩大 scope、权限、capability 或绕过 evidence publisher。

这保证“审查必发生、变化可追踪、错误可撤销、行为改变有门禁”，不保证模型生成的每条 Memory 或
Skill 天然正确。

## 5. 后续优先级

| 优先级 | 工作 | 原因 |
| --- | --- | --- |
| P0 | 直接使用交互 | 先补聊天附件、会话管理、首次使用引导和移动端闭环 |
| P0 | 公开 API auth/scope/authorization | 当前仍是本地单用户信任模型 |
| P1 | reranker + entailment gate | 直接提升检索排序和引用可靠性 |
| P1 | temporal fact ledger | 解决个人记忆与知识图谱的更新、冲突和过期 |
| P1 | GraphRAG communities + global/DRIFT | 补齐跨文档主题综述和探索式查询 |
| P2 | durable interactive Run events/cursor | 支持断线恢复和长任务继续 |
| P2 | 更多 connectors/MCP | 扩大个人知识来源，但不改变核心架构 |
| 延后 | MH-015/016/017 | 保留设计，不再优先于用户可感知的 Agent 平台交互 |

## 6. 不采用的做法

- 不再引入 LangGraph、LlamaIndex Agent、Haystack Agent 或 RAGFlow Agent 作为第二主循环。
- 不把每次成功轨迹直接拼接进系统 Prompt。
- 不让后台 review 直接写 HermesGraph Active Policy。
- 不让 Pattern 添加工具、扩大 scope 或修改证据阈值。
- 不因开源项目有某功能就整包迁移；只提取与现有 ownership 边界兼容的能力。

# HermesGraph 文档

这是项目的产品和技术设计入口。

- [PRD](./PRD.md)：产品定位、用户故事、功能范围、指标、路线图和验收标准。
- [研发智能 Agent 完整交付设计](./ENGINEERING_INTELLIGENCE_AGENT_DELIVERY.md)：交给 Luner 的实施主文档，
  固定研发团队业务主线、个人学习兼容、体验规格、模拟企业知识库、分阶段门禁与最终 Definition of Done。
- [技术实现文档](./TECHNICAL_DESIGN.md)：架构、数据模型、接口、检索、Agent runtime、自学习、评测、安全和逐步实施清单。
- [使用指南](./USER_GUIDE.md)：启动、会话恢复、上下文隔离、显式记忆、知识入库、反馈和失败恢复。
- [Agentic RAG 冻结基线](./AGENTIC_RAG_LOCK.md)：当前能力、成熟度、事实边界、冻结范围、
  `RAG-001` 至 `RAG-010` 后续任务和生产级恢复门槛。
- [开源 Agentic RAG 能力差距](./OPEN_SOURCE_AGENTIC_RAG_GAP_ANALYSIS.md)：GraphRAG、LightRAG、
  KAG、RAGFlow、LlamaIndex、Haystack、Graphiti、Mem0、Letta、Hermes 的能力对照和后续优先级。
- [MemoHarness 固定化记忆实施规划](./MEMOHARNESS_MEMORY_CONSOLIDATION_PLAN.md)：双层经验银行、
  D1-D6 诊断、Memory/Skill/Policy 三路固定化、run-scoped overlay、迁移、测试和分阶段验收。
- [Personal Control Plane](./PERSONAL_CONTROL_PLANE.md)：Task/Plan/Note、Persona、Day Archive、
  自然语言 Memory 纠错、Emotion、Hermes tools、API、持久化和工作台合同。
- [Intent Lock](./INTENT.md)：北极星目标、不可漂移约束和完成定义。
- [Progress](./PROGRESS.md)：已验证产物、审计修正、风险和下一检查点。
- [参考项目源码对比](./REFERENCE_PROJECT_COMPARISON.md)：ChatTutor、Desktop-Claw 与 HermesGraph
  的代码事实、功能矩阵和未完成项。
- [ADR-007](./ADR-007-framework-boundary.md)：历史 OpenAI Agents SDK/LangChain 边界，已被 ADR-008 部分取代。
- [ADR-008](./ADR-008-hermes-first-runtime.md)：Hermes-first 在线运行时、Capability Bridge 与双轨学习治理。
- [ADR-009](./ADR-009-remove-openai-agents-fallback.md)：删除 OpenAI Agents SDK fallback，保留 OpenAI Python SDK 模型原语。
- [ADR-010](./ADR-010-hermes-019-native-review-lifecycle.md)：Hermes 0.19、幂等发布、正常
  finalizer、每回合原生 review 和 completion 握手。
- [ADR-011](./ADR-011-harness-pattern-governance.md)：Pattern Evaluation/Promotion Evidence/
  Transition 三账本、状态机、稳定 Canary 分桶与 bounded `RunExecutionPolicy`。

推荐阅读顺序：先读 Intent Lock，再读研发智能 Agent 完整交付设计，然后按其指定章节阅读 PRD、
技术实现文档、Agentic RAG 冻结基线和 MemoHarness 专项规划，最后对照 Progress 执行。模拟企业
知识库位于 `examples/enterprise_knowledge/`；当前完整 Docker 成品使用仓库根目录的
`scripts/docker_up.sh` 启动。

# ADR-008: Hermes-first 在线运行时

状态：Accepted

日期：2026-07-20

## 背景

HermesGraph 已经实现可审计的 Agentic RAG、Qdrant/Neo4j 证据层、Postgres durable learning、
多模态摄取和 OpenAI 模型能力，但原先由 OpenAI Agents SDK 承担在线循环，Hermes 式 Memory/Skill
需要由项目自行复刻。当前产品目标是成熟的个人自学习 Agent，而不是继续维护另一套相对年轻的
会话、技能、记忆和后台回顾运行时。

## 决策

1. `hermes-agent==0.18.2` sidecar 是唯一在线 Agent Loop，负责会话、工具循环、Todo、原生
   Memory/Skill 和后台回顾。
2. HermesGraph FastAPI 保持系统记录与治理边界。Hermes 只能通过受信任
   `hermesgraph-bridge` 插件调用 run-scoped 能力，不能直连 Qdrant、Neo4j、Postgres 或自由 Shell。
3. OpenAI Python SDK 继续直接承担 Responses、Vision、Structured Outputs、Embeddings、hosted
   Web Search 和后台模型任务。迁移期 OpenAI Agents SDK fallback 已由 ADR-009 正式删除。
4. LangChain 继续承担 loader、splitter、retriever、LCEL 数据流、结构化转换、重试、adapter 和
   callbacks，不创建或包裹第二个 Agent Loop。
5. 最终答案必须由 Hermes 调用 `hermesgraph_publish_answer`。HermesGraph 使用本轮 evidence
   allowlist hydrate citation；Hermes 仅返回 evidence ID，不能伪造 URI、页码、trust 或 scope。
6. Hermes 原生 Memory/Skill 写入允许在持久 profile 中生效，但插件 hook 必须把成功变更镜像成
   `LearningChangeSet(state=native_applied, evaluation_status=requires_audit)`。影响检索、图谱、
   Prompt、安全或外部副作用的资产仍走 HermesGraph 的评测、晋级和回滚控制面。

## 安全边界

- 每个 run 使用不可预测 bridge ID；稳定会话记忆键由 tenant/project/user/session 通过 HMAC 派生，
  不向 Hermes 或模型暴露原始作用域。
- Hermes API 与内部 bridge 使用两个独立 secret；生产/预发布环境要求至少 32 字符。
- bridge 强制工具总预算、分工具预算、重复调用检测、schema、scope、timeout 和发布后禁用工具。
- 只启用 Hermes 原生 `memory`、`skills`、`todo` 与受信任插件；terminal、file、browser、delegation、
  session search 和 Hermes 自带 web tool 默认关闭。
- 任意 approval request 默认拒绝；需要副作用工具时必须在 HermesGraph 增加显式权限和审批合同。

## 后果

- 产品直接复用 Hermes 已成熟的个人 Agent 学习形态，减少重复建设。
- HermesGraph 的差异化集中在私有知识、图谱、证据、作用域、评测和可回滚治理，而不是复刻 Hermes。
- 运行时成为独立容器和版本化依赖，需要 sidecar 健康检查、持久卷、插件 contract test 与升级门禁。
- 原生学习是“先应用、后审计”，与 HermesGraph 高影响资产的“先评测、后晋级”并存；UI 和审计必须
  清楚展示两者，不能把 native write 误报为已通过 HermesGraph 评测。
- 2026-07-22 接受 ADR-009：删除 OpenAI Agents SDK runtime、session adapter、配置值和依赖；
  `offline` 仅用于 deterministic 测试，不是第二个在线 Agent。

# ADR-009: 删除 OpenAI Agents SDK Fallback

日期：2026-07-22

状态：Accepted

## 背景

ADR-008 已将 Hermes Agent 0.18.2 设为唯一在线 Agent Loop。迁移期仍保留了
`OpenAIAgentsRuntime`、`RUNTIME_MODE=openai`、SDK session/compaction 和 `openai-agents` 依赖，
用于 Hermes 尚未稳定时回退。此后 Hermes sidecar、run-scoped Capability Bridge、严格证据发布、
原生 Memory/Skill 审计、条件回滚和快照生命周期均已形成完整后端闭环。

继续保留第二运行时会产生四类成本：重复工具封装与预算逻辑、额外 session/tracing 配置、依赖与供应链
面积、以及未来误启用双循环的架构漂移风险。它已经不再提供与成本相称的恢复价值。

## 决策

1. 删除 `OpenAIAgentsRuntime`、Agents SDK session adapter 和对应测试。
2. 删除 `RUNTIME_MODE=openai`；合法值只剩 `hermes` 与 `offline`。
3. 删除 `openai-agents` 及其独占传递依赖。
4. 保留官方 `openai` Python SDK。Responses、Structured Outputs、Vision、Embeddings、hosted Web
   Search、图谱抽取、检索规划和结构化反思继续通过它实现。
5. 保留 deterministic `offline` runtime，用于无模型密钥的单元测试、回放和本地演示；它不允许用于
   生产在线流量，也不构成第二个模型 Agent Loop。
6. LangChain 继续只拥有 loader、splitter、retriever、Runnable 数据流、结构化转换、重试、adapter
   和 callbacks，不创建 Agent。

## 结果

- 在线编排只有 Hermes 一条路径，配置错误不能静默切换到另一个模型循环。
- OpenAI-compatible provider 仍通过 `AsyncOpenAI(base_url=...)` 工作，不依赖 Agents SDK。
- Hermes 故障恢复依赖 sidecar 重启、持久 profile、bridge 幂等、快照回滚和版本升级 contract，
  不再依赖切换框架。
- 历史 OpenAI Agents SDK 评测结果继续作为迁移记录保存，但不代表当前可执行能力。

## 重新评估条件

只有在 Hermes 无法满足经过量化的核心需求，并且新运行时通过完整 contract、scope、安全、证据发布、
学习审计和迁移评测时，才允许提出新的 ADR。不得仅为了“备用”重新加入第二 Agent SDK。

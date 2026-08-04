# ADR-007: OpenAI Agents SDK 与 LangChain 的运行边界

状态：Superseded by ADR-008

日期：2026-07-13

2026-07-20 起，本 ADR 的“OpenAI Agents SDK 为在线运行时”部分由
[`ADR-008`](./ADR-008-hermes-first-runtime.md) 取代；2026-07-22 起迁移 fallback 又由
[`ADR-009`](./ADR-009-remove-openai-agents-fallback.md) 删除。本文仅保留历史决策证据。LangChain
不创建第二个在线 Agent Loop 的边界仍然有效。

## 决策

OpenAI Agents SDK 拥有在线会话、工具循环、handoff、guardrail、approval、session 和 Agent trace。LangChain 拥有中间组件标准、Runnable 数据流、文档处理、检索器组合、Prompt 渲染、结构化转换、provider adapter 和 callbacks。

在线用户请求不得同时经过 OpenAI `Runner` 和 LangChain `create_agent`。LangChain Runnable 可以被 SDK function tool 调用；后台 ingestion、reflection 和 eval 可以单独调用 Runnable，但不能接管在线会话控制权。

## 原因

两个框架都具备 Agent Loop。若互相嵌套，会造成停止条件、工具预算、状态、trace 和错误恢复存在两个真相源。采用“一个控制平面、一个集成平面”后，框架职责清晰，同时保留 LangChain 的广泛组件生态。

## 后果

- 不在在线代码中引入 `langchain.agents.create_agent`。
- 不用 LangGraph 编排同一个用户请求。
- 所有 LangChain 输出先转换为领域合同，再返回 SDK tool。
- 所有 Runnable 调用携带 run metadata 和 callbacks。
- durable workflow 若需要，另选 Temporal、DBOS 或 Dapr。

# Relay Agent Runtime 设计

状态：Active
版本：2026.08
Owner：Agent Experience

## 运行模型

Relay 将每次请求建模为有界 Run，而不是无限自主循环。Run 状态包含用户目标、最近 8 轮会话、最多
12,000 字符上下文、工具预算、证据白名单和取消令牌。模型负责在允许的能力中选择下一步，Capability
Bridge 负责 scope、参数和输出大小校验。

Capability Bridge 是 Relay 与所有工具之间的强制执行边界；任何工具调用都不能绕过该组件。

## 路由和循环

普通问候走 conversation fast lane，不调用知识工具。知识问题至少执行一次锚定检索；依赖关系问题在
文本证据基础上调用 Constellation。默认最多 8 个工具调用、4 个检索子查询和 2 轮检索。满足证据覆盖、
预算耗尽、用户取消、连续工具无新增证据或模型发布回答时停止。

所有工具返回结构化 envelope：`status`、`summary`、`evidence_ids`、`retryable` 和有界 payload。模型看不
到数据库凭据、任意文件系统和原始内部异常。最终回答只能引用本 Run 工具返回的 evidence ID；未知
引用或未授权 memory ID 会在 Bridge 发布阶段被拒绝。

## 可恢复性

Run 和公开事件先写 PostgreSQL，再通过 SSE 游标发送。浏览器断开不取消运行；重连携带最后事件序号。
进程在 provider 调用中重启时，将不可恢复的 coroutine 标记为 `run_interrupted`，用户可从保留的输入
重新运行。工具本身必须支持幂等键，行动类工具在重试前检查既有结果。

## 学习边界

完成轨迹异步交给 Learning Worker。反思只能生成候选 Memory、Skill patch 或 Pattern，不得直接改动
运行时 Prompt。候选经过作用域、证据、回放和提升门禁后才能启用；负反馈会降低候选置信度并触发复审。

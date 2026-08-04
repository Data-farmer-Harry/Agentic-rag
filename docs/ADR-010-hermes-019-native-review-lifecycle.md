# ADR-010: Hermes 0.19 Native Review Lifecycle

状态：Accepted
日期：2026-07-30

## 决策

1. Hermes Agent 固定升级到 `0.19.0`，继续作为唯一在线 Agent Loop。
2. HermesGraph 显式传入同 scope 的 bounded `conversation_history`，不依赖 bridge 临时 session
   自行积累历史。
3. `hermesgraph_publish_answer` 的首个结果是不可变公开 artifact；重复 publish 返回幂等成功，
   不覆盖答案、不重复事件。
4. 首个发布后立即向用户返回，但不调用 `/stop`。Hermes 主 loop 正常结束，才能触发 0.19 原生
   Memory/Skill background review。
5. `memory.nudge_interval=1`、`skills.creation_nudge_interval=1`。由于每个应用 run 使用隔离 bridge
   session，每个 Agent 回合都必须独立触发原生 review。
6. background review 通过父 `session_id` 关联 bridge；其临时 `task_id` 不作为业务 run 身份。
7. Hermes 0.19 的 `bg-review` 线程在 `on_session_end` 发送 completion event。应用在收到该事件或
   `HERMES_NATIVE_REVIEW_TIMEOUT_SECONDS` 到期前保留 bridge state。
8. 原生 Memory/Skill 仍是 Hermes 的资产；HermesGraph 只保存写前快照、脱敏审计、接受和条件回滚。

## 原因

Hermes 插件工具注册没有 `return_direct`、`terminal` 或 `stop_after_call` 元数据。首个 publish 后立即
调用 `/stop` 虽能减少模型回合，却会绕过正常 turn finalizer，导致 Hermes 原生 background review
无法可靠启动。反过来，只等待 gateway 的 `run.completed` 也不够：0.19 在主 run 完成后启动 daemon
review，迟到的 native tool audit 会在 bridge 被释放后收到 404。

因此需要“前台 artifact 立即返回 + 后台 run 正常收尾 + review completion 握手”的双阶段生命周期。

## 不变量

- 用户响应不等待 background review。
- 第一次发布之后任何业务工具都被拒绝。
- 重复 publish 不改变首次 artifact。
- review 写入必须有 snapshot 和 run/scope 关联。
- completion 丢失只导致 bounded retention timeout，不阻塞用户或永久泄漏 bridge。
- 模型网关失败不能被描述为 Hermes/bridge 失败；API 层应保留可分类错误。

## 验证

- Hermes `/health` 返回 `version=0.19.0`。
- 真实 Agent run 在首次 publish 后约 12 秒返回；Hermes 随后以
  `text_response(finish_reason=stop)` 正常结束。
- review fork 随后调用 `memory`、`skills_list`、`skill_manage` 并完成。
- 单元测试覆盖发布幂等、history 注入、review completion、父 session 关联和 bridge 延迟释放。
- 当前模型网关随后返回 `429 model_cooldown`，因此修正后的最终 live 握手由无模型契约验证；
  provider 恢复后需再跑一次纵向检查。

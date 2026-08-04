# ADR-007：模型路由与受控降级

状态：Accepted  
日期：2026-03-08  
修订：2（2026-06-11）  
Owner：Architecture Council

## 决策

所有模型调用由 Prism 承担。Relay 选择任务 lane，不直接选择 provider。当前 lane 是
`fast_conversation`、`grounded_answer`、`structured_extraction` 和 `vision_analysis`。

只有以下传输型失败可以使用已配置 fallback：

- provider timeout。
- HTTP 429。
- provider HTTP 5xx。

以下结果禁止 fallback：

- 安全或内容策略拒绝。
- 证据不足。
- Structured Output schema 校验失败。
- 调用方没有权限。
- 输入包含待分析的提示注入。

## 原因

Fallback 用于保持基础可用性，不是绕过安全和质量约束。证据不足时更换模型不会产生新证据；安全
拒答后换 provider 会形成 policy shopping。

## 运行约束

lane、模型 revision、重试次数和 timeout 在 run start 冻结。Prism 返回稳定错误码，Relay 决定面向
用户的中文提示。任何 provider 原始 URL、凭据状态和内部响应正文都不能进入最终答案。


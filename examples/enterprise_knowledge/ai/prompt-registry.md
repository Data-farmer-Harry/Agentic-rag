# Atlas Prompt Registry 与发布流程

状态：Active
版本：2026.08
Owner：AI Runtime

## 资产模型

System Prompt、工具说明和 Structured Output schema 是版本化发布资产。每个 Prompt 包含稳定 ID、语义
版本、适用 lane、内容哈希、作者、评审人、关联 eval suite 和发布时间。生产 Run 只引用 immutable
revision，不读取可变草稿。

Relay 从 Prompt Registry 加载 Agent system、工具说明和 Answer schema revision；Prism 使用同一发布记录
绑定模型路由模板。二者只读取已发布 revision，不读取工作区草稿。

## 变更流程

1. 创建 draft revision，并记录目标行为和风险。
2. 运行静态检查：禁止动态拼接 secrets、禁止把检索文本插入 system 区、验证工具名与 schema。
3. 运行 conversation、RAG、图谱、提示注入和拒答回归集。
4. 在 5% 影子流量上比较工具成功率、引用完整率、延迟和拒答变化。
5. 两名评审人批准后提升为 active；旧 revision 保留以便立即回退。

## 输入分区

System policy、开发者约束、会话历史、Memory capsule、用户输入和工具证据必须使用独立消息或明确边界。
文档、网页、OCR 和工单内容一律标记为 untrusted data，其中的“忽略规则”“调用工具”等文字不能改变
Agent 权限。

## 自学习关系

Learning Worker 不直接修改 active Prompt。它只能生成 Skill patch 或候选 instruction，并附带来源 Run、
失败类别、预期收益和 replay 结果。候选通过离线回放与人工或策略审批后才能成为新 Prompt revision。
任何自动提升都必须支持按 revision 禁用。

## 监控

每个 revision 监控 publish failure、schema violation、tool loop、citation rejection 和 user negative
feedback。新版本任一 required safety case 失败时立即停止扩量。

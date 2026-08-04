# Northstar Labs Atlas 企业研发知识库样例

本目录是完全虚构的企业内部研发知识库，用于 HermesGraph 的产品演示、检索评测、知识图谱抽取和
回答质量测试。Northstar Labs、Atlas、所有团队、服务、事件编号和模型名称均为虚构，不对应真实
公司、系统、人员或凭据。

## 使用目标

- 验证直接事实检索。
- 验证跨文档综合和服务依赖路径。
- 验证 ADR supersedes 与当前有效事实。
- 验证事故、根因、缓解和 Runbook 的关系。
- 验证无答案时拒绝编造。
- 验证文档中的提示注入只能作为数据，不能成为 Agent 指令。

## 知识边界

Atlas 是 Northstar Labs 的智能研发平台。团队知识默认是 `private/verified`；
`security/untrusted-ticket-export.md` 是唯一故意标记为 `untrusted` 的安全样本。默认评测不加载
历史 arXiv 论文，避免公共论文淹没企业内部事实。

`manifest.json` 是导入真相源；`graph_seed.json` 是人工审阅、来源可追溯的最小演示子图；
`evaluation/golden_questions.json` 是回答级门禁。fixture importer 必须复用现有 durable
ingestion，不要为样例创建平行文档仓。规则抽取产生的其他候选仍保持 pending，不能因导入样例而
自动升格为事实。

# Atlas 测试策略

状态：Active
版本：2026.08
Owner：AI Quality

## 测试金字塔

Domain 和 controller 使用快速单元测试；repository、provider 和 Capability 使用合同测试；PostgreSQL、
Qdrant、Neo4j、Hermes 使用少量真实集成测试；浏览器纵向覆盖普通聊天、知识查询、上传、取消、刷新恢复
和移动端布局。模型质量由独立 eval suite 负责，不能用快照单测代替。

## 必测不变量

- tenant/project/user scope 不能越界。
- archived/superseded 文档不进入当前事实。
- 工具和回答只能引用当前 Run 白名单证据。
- outbox 重试幂等，迟到 revision 不复活旧数据。
- 浏览器断开不取消 Run，显式取消会停止后续工具。
- Memory 撤回后不再进入新 Prompt。

## Fixture

企业 fixture 是完全虚构、版本化的测试数据。required case 固定来源、事实、禁止结论和图路径。测试运行
可以 reset fixture，但不得删除同工作区的用户资料。每次 manifest revision 变化都重新编译 retrieval
fixture 并记录 provenance。

## 非确定模型

Provider 测试分为 schema gate、固定少量 live case 和离线 replay。Live 失败按 authentication、timeout、
429、5xx 和 contract violation 分类，不能用无限重试获得好看分数。报告记录模型与 Prompt revision。

## 合并标准

受影响模块测试必须通过，required eval 不得退化，Ruff/mypy/build 全绿。涉及共享 scope、存储迁移或
回答合同的变更需要扩大回归范围。

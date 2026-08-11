# Atlas 平台威胁模型

状态：Active
版本：2026.08
Owner：Security Engineering

## 信任边界

公网客户端只信任 Gatehouse；Gatehouse 与内部服务通过 mTLS；模型 provider、用户上传文档、网页搜索
结果和 OCR 文本都属于不可信边界。PostgreSQL 是业务真相源，Qdrant/Neo4j 是受 scope 约束的投影，
不能承担授权判断。

## 主要威胁

1. 客户端伪造 `tenant_id` 或 `user_id` 获取跨租户数据。
2. 检索文档通过提示注入诱导 Agent 泄露 secret 或调用写工具。
3. 模型生成任意 SQL/Cypher/filter 绕过数据范围。
4. 恶意文件利用 parser、OCR 或压缩包造成代码执行或资源耗尽。
5. 依赖或容器供应链被篡改。
6. 自学习把一次错误或攻击内容固化成长期 Memory/Skill。

## 缓解

Scope 由 EdDSA token 经 Gatehouse 服务端绑定并在每个 repository 下推。工具使用 allowlist 和结构化
schema，数据库查询由服务实现；模型只提供受限参数。上传在隔离 worker 中解析，限制解压比例、页数、
像素和 CPU 时间。生产镜像固定 digest、生成 SBOM 并验证签名。

检索文本放入 untrusted data 区，发布答案时验证 evidence ID。写操作要求确认；Learning 默认为 shadow，
候选必须有来源 Run、回放和权限不扩张检查。

## 剩余风险

模型仍可能错误综合可信证据，因此高影响运维建议必须引用 Runbook 并要求人工确认。新 parser 零日漏洞
通过进程隔离、只读文件系统和无网络 sandbox 降低影响，但不能完全消除。

# Atlas 系统架构总览

状态：Active  
版本：2026.06  
Owner：Architecture Council

## 系统目标

Atlas 是 Northstar Labs 的智能研发平台，为内部工程团队提供会话、知识检索、知识图谱和模型调用。
平台按同步请求链、异步知识链和治理链分层。任何服务都不能绕过 Gatehouse 直接向公网暴露接口。

## 核心服务

| 服务 | 职责 | Owner |
| --- | --- | --- |
| Gatehouse | API Gateway、请求校验、租户限流、访问令牌验证 | Edge Platform |
| Sentinel | 身份、令牌签发、JWKS 发布和密钥轮换 | Trust Foundations |
| Relay | 会话编排、意图路由、工具调用和最终回答协调 | Agent Experience |
| Polaris | 团队知识的 dense+sparse 混合检索和 workspace 过滤 | Knowledge Systems |
| Constellation | 受控知识图谱查询和证据路径组装 | Knowledge Systems |
| Foundry | 文档解析、分块、索引和图谱候选生产 | Knowledge Systems |
| Prism | 模型选择、预算、重试和 provider 降级 | AI Runtime |

## 同步回答链

客户端只连接 Gatehouse。Gatehouse 使用 Sentinel 发布的 JWKS 在本地验证访问令牌，然后把已绑定的
workspace 与 user context 转发给 Relay。Relay 先判断输入是普通会话、知识问题、图谱问题还是行动
请求。知识问题调用 Polaris；关系和影响问题在 Polaris 证据基础上调用 Constellation；需要模型时
统一通过 Prism。Relay 负责把最终答案、引用、限制和公开运行事件返回给 Gatehouse。

```text
Client -> Gatehouse -> Relay -> Polaris -> Qdrant/PostgreSQL
                         |        |
                         |        +-> Constellation -> Neo4j
                         +-> Prism -> approved model providers
```

## 异步知识链

Foundry 接收上传对象后保存原件，生成 Document IR 和 token-aware Chunk。文档与 Chunk 元数据写入
PostgreSQL，向量和稀疏索引写入 Qdrant collection `atlas_chunks_v3`，结构关系和经过审核的语义关系
投影到 Neo4j。Foundry 不负责在线回答，Relay 也不直接解析文件。

## 数据边界

所有请求和索引对象必须携带 `tenant_id`、`workspace_id` 和来源层。团队内部资料优先于个人资料，
个人资料优先于公共参考资料，但来源优先级不能覆盖文档版本和显式 supersedes。公共资料不得用于
回答内部负责人、事故细节或安全配置。

## 关键不变量

1. Gatehouse 是唯一公网入口。
2. Sentinel 是身份与签名密钥的唯一所有者。
3. Relay 是会话和工具编排的唯一所有者。
4. Polaris 不生成最终答案，Constellation 不执行任意 Cypher。
5. Foundry 只生产可追溯知识对象和候选，不自动批准语义事实。
6. Prism 不持久化业务知识，也不接受调用方直接指定 provider secret。


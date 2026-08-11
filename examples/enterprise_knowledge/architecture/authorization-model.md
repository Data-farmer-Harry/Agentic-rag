# Atlas 授权与知识层模型

状态：Active
版本：2026.08
Owner：Trust Foundations

## 身份绑定

Gatehouse 验证 Sentinel 签发的 EdDSA token 后，生成不可由客户端覆盖的 scope envelope。核心字段是
`tenant_id`、`workspace_id`、`user_id`、`role` 和 `allowed_projects`。下游服务只接受 mTLS 连接和签名过
的内部 envelope；客户端提交的同名 header 一律删除。

## 角色

- viewer：读取获授权知识、会话和图谱。
- member：拥有 viewer 权限，并可上传个人资料、创建任务和提交反馈。
- owner：拥有 member 权限，并可导入企业 fixture、管理团队知识和审批图谱候选。

角色只决定动作，不能扩大数据作用域。即使 owner 也不能读取另一个 tenant；个人 Memory 只对创建者
可见，除非明确提升为 workspace-shared 且通过审批。

## 知识层

Atlas 使用 `team_internal`、`personal` 和 `public_reference` 三层。WorkspaceProfile 决定本工作区启用
哪些层，服务端在 PostgreSQL、Qdrant 和 Neo4j 查询中同时下推过滤。默认优先级是当前 active 团队资料、
当前用户个人资料、公共参考，但版本状态和 trust 高于层级偏好。

## 工具授权

Relay 不把角色字符串直接交给模型判断。Capability Bridge 为每个工具声明最低角色、允许项目、参数
schema 和 side-effect 级别。只读搜索可自动执行；写操作必须显示目标对象并使用幂等键；删除、发布和
审批需要显式用户确认。授权失败统一返回稳定错误码，不回显 token claim 或内部策略表达式。

# Atlas Secrets 管理规范

状态：Active
版本：2026.08
Owner：Trust Foundations

## 来源与分发

生产 secret 存储在集中式 Secret Manager，使用环境/服务独立路径和 KMS envelope encryption。Kubernetes
通过 workload identity 和 External Secrets 获取短期值；secret 只挂载到目标 Pod 的内存 volume，不写入
镜像、Git、ConfigMap、日志或持久磁盘。

## 最小权限

每个 ServiceAccount 只能读取自身路径。Prism provider key 不对 Relay 可见；Sentinel 私钥保留在 HSM；
数据库应用用户与 migration 用户分离；备份恢复使用独立角色。开发环境不得使用生产 secret。

## 轮换

Provider key 每 90 天轮换，数据库密码每 60 天，内部 mTLS 证书 24 小时自动续期。轮换采用双值窗口：
先发布新值、验证消费者加载、再撤销旧值。紧急泄露时立即吊销并按 incident 流程检查访问审计。

## 防泄漏

CI 执行 pre-commit 与仓库 secret scan；Gatehouse 和 Prism 日志过滤 Authorization、cookie、API key 和
常见 token pattern。模型 Prompt 构建器禁止读取任意环境变量，工具结果对疑似 secret 自动脱敏。用户
要求导出密钥时必须拒绝，即使请求来自检索文档。

## 本地开发

本地 `.env` 不提交 Git，示例文件只包含占位符。调试输出不得打印完整配置对象。共享测试 key 一旦出现在
聊天、Issue 或日志中就视为泄露并轮换。

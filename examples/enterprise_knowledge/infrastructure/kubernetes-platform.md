# Atlas Kubernetes 平台规范

状态：Active
版本：2026.08
Owner：Cloud Platform

## 集群布局

生产集群跨三个可用区，每个服务使用独立 ServiceAccount 和 namespace-scoped RBAC。Gatehouse、Relay、
Polaris、Constellation、Foundry、Prism 至少两个副本，使用 topology spread constraints 分散到不同
zone。数据库由专用 operator 或托管服务运行，不与批量 embedding worker 共用节点池。

## 探针与终止

`startupProbe` 只判断进程是否完成初始化；`readinessProbe` 检查接收新流量所需依赖；`livenessProbe`
不得因为下游 provider 暂时不可用而重启服务。Pod 收到 SIGTERM 后先从 ready endpoint 摘除，等待最多
30 秒完成活跃请求，再取消剩余 Run 并记录 `run_interrupted`。

## 资源

所有容器必须声明 request/limit。在线服务 CPU request 按 P50、limit 按 P99 峰值设置；Foundry OCR 和
embedding worker 使用独立队列与 autoscaler。HPA 主要观察 CPU、in-flight requests 和队列延迟，禁止
只按内存扩缩。

## 安全

容器默认只读根文件系统、非 root 用户、禁用 privilege escalation，并应用 seccomp RuntimeDefault。
NetworkPolicy 只允许声明的服务边，例如 Relay 可访问 Polaris/Constellation/Prism，但不能直连 Qdrant
或 Neo4j。Secret 通过 External Secrets 挂载到内存 volume，不写入镜像和 ConfigMap。

## 发布保护

PodDisruptionBudget 保证在线服务至少 50% 可用。滚动发布 `maxUnavailable=0`、`maxSurge=25%`；readiness
未通过的版本不得接流量。数据库 migration Job 必须在应用发布前完成兼容性 preflight。

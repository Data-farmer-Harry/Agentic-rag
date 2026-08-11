# Atlas CI/CD 发布流水线

状态：Active
版本：2026.08
Owner：Developer Productivity

## Pull Request 门禁

每个 PR 执行格式检查、静态类型、单元测试、合同测试、前端 production build、依赖漏洞扫描、secret
扫描和容器 SBOM 生成。修改 Prompt、检索或图谱逻辑时必须运行对应 required eval；修改 migration 时
必须验证上一版本数据库的 expand/rollback 路径。

## 构建

镜像在隔离 runner 中构建，基础镜像固定 digest，依赖使用 lockfile。构建产物生成 CycloneDX SBOM，
使用 keyless signing 签名并写入 provenance。部署系统只接受来自受信 CI identity、签名有效且扫描无
critical 漏洞的镜像。

## 环境推进

同一镜像依次进入 integration、staging 和 production，禁止各环境重新 build。Staging 执行 API smoke、
企业知识 10-case、数据库 migration、SSE 重连和取消测试。生产先部署 5% canary 15 分钟，再到 25%、
50%、100%。

## 自动停止条件

任一阶段出现 5xx 增加 1%、P95 延迟恶化 20%、citation rejection 翻倍、required eval 失败或
`run_interrupted` 异常增加，流水线自动停止扩量。回滚应用镜像不自动回滚数据库；数据库必须保持向后
兼容，破坏性 contract migration 在观察两个发布周期后单独执行。

## 审批

普通服务由代码 owner 批准；认证、授权、数据删除和 Prompt safety 变更需要对应领域第二评审人。紧急
发布可缩短 canary，但不能跳过签名、SBOM、migration preflight 和审计记录。

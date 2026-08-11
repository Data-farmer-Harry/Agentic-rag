# Atlas 软件供应链安全

状态：Active
版本：2026.08
Owner：Security Engineering

## 依赖

Python 与 Node 依赖使用 lockfile 和哈希；容器基础镜像固定到 digest。新增高权限依赖需要 owner 评审，
无法维护或来源不明的包不得进入核心服务。自动升级机器人只创建 PR，不直接合并。

## 构建可信度

CI runner 临时创建且无生产凭据。构建生成 CycloneDX SBOM、SLSA provenance 和 keyless signature。
Admission policy 校验镜像来自批准仓库、签名身份匹配、digest 未变且 critical 漏洞为 0；latest tag 禁止
部署生产。

## 第三方模型与工具

模型 provider SDK 经过 adapter 隔离，升级时运行 Structured Output、Tool Calling、timeout 和错误映射
合同。MCP 或外部工具必须登记 owner、数据范围、网络目的地和 side-effect，不允许模型临时安装插件。

## 漏洞响应

Critical 可利用漏洞的目标修复时间为 24 小时，High 为 7 天。无法立即升级时记录补偿控制、影响服务和
到期日。紧急重建仍必须生成 SBOM 和签名，不允许手工修改运行中容器。

## 验证

每周扫描镜像和依赖，每季度验证从源码到部署 digest 的 provenance。离线环境使用内部镜像和包代理，
但镜像同步不能跳过签名和哈希检查。

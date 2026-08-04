# Atlas 工程师第一周入职指南

版本：2026 Q3  
状态：Active  
Owner：Engineering Enablement

## 学习目标

第一周结束时，新工程师应能画出同步请求链和异步知识链，找到各服务 Owner，运行一个只读检索诊断，
并解释为什么当前 token 使用 EdDSA、Polaris 使用混合检索。

## 推荐顺序

### 第 1 天：平台地图与边界

阅读《Atlas 系统架构总览》和《请求与知识数据流》。重点理解 Gatehouse 是唯一入口、Relay 是唯一
会话编排者、Foundry 不回答问题、Polaris 与 Constellation 的职责区别。

### 第 2 天：身份和请求

阅读 Gatehouse、Sentinel 服务说明和 ADR-012。跟随 JWKS Rotation Runbook 完成只读演练，确认当前
算法是 EdDSA，而不是历史 ADR-009 的 RS256。

### 第 3 天：知识摄取与检索

阅读 Foundry、Polaris、ADR-003。观察一个样例文档从 Document IR 到 Qdrant/Neo4j 的状态，不修改
生产 collection。

### 第 4 天：图谱与模型路由

阅读 Constellation、Prism 和 ADR-007。运行 allowlisted ownership/incident path 查询，理解为什么
安全拒答和证据不足不能通过 fallback 绕过。

### 第 5 天：事故和运行

阅读 INC-2026-0218、INC-2026-0527 及对应 Runbook。与所属团队值班同学完成一次 table-top 演练，
把发现的问题记录为文档反馈，不直接修改 active ADR。

## 完成检查

- 能说出七个核心服务及 Owner。
- 能解释 Relay 何时不触发检索。
- 能列出 Polaris 退化时前三项检查。
- 能说明 ADR-012 为什么替代 ADR-009。
- 能从引用打开至少一个事故原文和一个 Runbook。


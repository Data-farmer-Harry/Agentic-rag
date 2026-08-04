# Relay 服务说明

状态：Active  
版本：3.1  
Owner：Agent Experience  
值班：Atlas Agent

## 职责

Relay 是 Atlas 的会话与 Agent 编排服务。它管理 conversation、run、幂等启动、SSE 事件、用户取消、
工具预算和最终答案发布。Relay 是唯一可以协调 Polaris、Constellation、Prism 和个人行动工具的服务。

Relay 不直接读取 Qdrant、Neo4j 或原始对象；所有知识访问必须通过受控服务合同。它也不接收调用方
提供的任意工具名、Cypher 或 provider key。

## 路由

- `conversation`：问候、确认、轻量连续对话；调用 Prism fast lane，不检索。
- `knowledge`：直接事实或文档问题；调用 Polaris。
- `graph`：依赖、负责人、事故关系、影响分析；Polaris 取证后调用 Constellation。
- `research`：比较或多来源综合；最多四个子查询、两轮检索。
- `action`：创建受控 Task/Plan/Note；写入前显示明确对象。

同一 conversation 默认读取最近 8 轮、最多 12,000 字符。历史必须按 tenant、workspace、user 和
conversation 四层隔离。

## 依赖

- Gatehouse：唯一入口和服务端 scope envelope。
- Polaris：文本知识和证据。
- Constellation：allowlisted 图谱路径。
- Prism：模型调用。
- PostgreSQL：run、event cursor、conversation metadata。

## 终态

`completed`、`failed`、`cancelled` 是公开终态。浏览器断开只取消订阅，不取消 run；用户必须显式调用
cancel。进程重启后无法恢复的模型 coroutine 标记 `run_interrupted`，不能永久停留在 running。


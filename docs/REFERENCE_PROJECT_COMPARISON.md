# ChatTutor / Desktop-Claw / HermesGraph 源码对比

审计日期：2026-07-29

## 1. 口径与结论

本报告基于两个仓库当日 `main` 源码归档做静态审计，不把 README 声明自动视为已实现：

- [DjTaNg-404/ChatTutor](https://github.com/DjTaNg-404/ChatTutor)
- [DjTaNg-404/Desktop-Claw](https://github.com/DjTaNg-404/Desktop-Claw)

2026-07-29 复核采用“用户可感知功能”口径：只比较聊天、教学、任务、计划、笔记、记忆、人格、
知识库、图谱、文件能力和桌面体验。测试数量、类型检查、容器、迁移、审计、证据门禁、回滚链路、
限流和可观测性不计入功能覆盖结论；只有形成真实用户入口的账号或恢复能力才计为产品功能。

结论不是“他们有的 HermesGraph 已经全有”。更准确的说法是：

1. HermesGraph 的 Agentic RAG、知识图谱在线检索、证据治理、durable learning 和 Skill
   评测/回滚明显更完整。
2. HermesGraph 已补齐通用 Task/Plan/Step/Checklist/Note；ChatTutor 仍独有苏格拉底教学状态机、
   学习时长/薄弱点/误区画像和多轮教学计划生成体验。
3. HermesGraph 已补齐 Persona/Onboarding、Day Archive/Diary/Calendar 和确定性 Emotion；
   Desktop-Claw 仍独有 Electron 悬浮球、拖放、快速输入和跨平台桌面分发。
4. Desktop-Claw 的文件写改删不作为 HermesGraph 的“缺失功能”；当前产品安全边界明确禁止
   Agent 任意修改或删除本机文件。

## 2. ChatTutor 代码事实

### 2.1 Agent 与 RAG

- 多角色工作流真实存在，但本质是共享一个模型的 LangGraph 节点和 Prompt：Analyzer 决定
  Tutor/Judge/Inquiry/Plan/Aggregator 路由，并不是多个独立 Agent Runtime。参见
  [`agent_builder.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/core/agent_builder.py)。
- 工具面主要是百度搜索；工具调用后再生成一次回答，不是开放式多轮 ReAct 工具循环。
- 在线 RAG 索引的是历史问答，使用 task-scoped FAISS；它不是通用个人文档知识库。参见
  [`vector_store.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/core/vector_store.py) 和
  [`context_rag.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/core/context_rag.py)。

### 2.2 知识图谱与“自学习”

- 图谱在会话后通过独立接口构建，使用内存 `NetworkX` 并导出 JSON/HTML；在线回答不调用该图，
  因而不是 GraphRAG。参见
  [`kg_builder.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/kg/kg_builder.py) 和
  [`kg.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/api/kg.py)。
- “学习画像”是从对话中用规则提取目标、偏好、薄弱点、误区和进展，再注入 Prompt。代码中没有
  Skill discovery、版本、离线回放、晋级或回滚。参见
  [`learning_profile.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/core/learning_profile.py)。
- 因此 ChatTutor 的这部分应称为个性化画像记忆，不能等同于 Hermes 式自进化 Skill。

### 2.3 真正值得借鉴的能力

- 任务 CRUD、每日/任务笔记、时间线、总结和 checklist 已有实际后端。
- 学习计划有多轮状态，支持收集目标、生成、确认、暂停和继续。参见
  [`task_plan/dialog.py`](https://github.com/DjTaNg-404/ChatTutor/blob/main/app/core/task_plan/dialog.py)。
- Web 控制台和 PyQt 桌宠提供了更强的教学产品表达，但语音客户端、设置保存等仍有未闭环部分。

## 3. Desktop-Claw 代码事实

### 3.1 Agent、Prompt 与 Skill

- 存在最多 10 轮的 function-calling ReAct-like loop。模型接口是手写 OpenAI-compatible
  `/chat/completions` SSE 客户端，不是 OpenAI SDK。参见
  [`loop.ts`](https://github.com/DjTaNg-404/Desktop-Claw/blob/main/packages/backend/src/agent/loop.ts)。
- Base、SOUL、USER、CONTEXT、Skills、BOOTSTRAP 六层 Prompt 组装真实存在。参见
  [`prompt-assembler.ts`](https://github.com/DjTaNg-404/Desktop-Claw/blob/main/packages/backend/src/agent/prompt-assembler.ts)。
- Skill manager 支持脚本和 reference，但当前内置 File/Memory Skill 默认自动激活，通用 Skill
  目录没有进入主加载路径；脚本继承完整进程环境，也没有离线评测、权限晋级或回滚。参见
  [`skill-manager.ts`](https://github.com/DjTaNg-404/Desktop-Claw/blob/main/packages/backend/src/agent/skill-manager.ts)。

### 3.2 记忆与连续感

- 对话按天写 JSON，日终生成 summary、facts 和第一人称 diary，并编译 USER/CONTEXT。参见
  [`memory-service.ts`](https://github.com/DjTaNg-404/Desktop-Claw/blob/main/packages/backend/src/memory/memory-service.ts)。
- 后台 interpret 会定期抽取结构化记忆，但调用前清空 buffer，模型失败时不会重新入队；原始日归档
  仍保留。参见
  [`interpret-service.ts`](https://github.com/DjTaNg-404/Desktop-Claw/blob/main/packages/backend/src/memory/interpret-service.ts)。
- maintain 的索引重建、衰减和新鲜度检查存在，但 Topic 合并和 Self 冲突清洗仍是待实现。
- 记忆检索主要是 label/summary 子串匹配，不是向量 RAG，也没有知识图谱。

### 3.3 文件与桌面

- PDF、DOCX、XLSX 读取真实存在，同时支持文件创建、编辑和删除。参见
  [`read_file.ts`](https://github.com/DjTaNg-404/Desktop-Claw/blob/main/packages/backend/src/agent/skills/file/scripts/read_file.ts)。
- 当前文件上限只有 512 KB，写路径存在父目录符号链接逃逸风险，写删也没有 ChangeSet、回滚或审计。
- Electron 悬浮球、日历、拖放和 macOS/Windows 打包是 Desktop-Claw 最鲜明、也是 HermesGraph
  当前没有的产品能力。

## 4. 功能矩阵

| 能力 | ChatTutor | Desktop-Claw | HermesGraph |
| --- | --- | --- | --- |
| 在线 Agent Loop | LangGraph 教学路由 | 10 轮 ReAct-like | Hermes 0.19.0 唯一在线 Loop |
| 通用文档 RAG | 部分：历史问答 FAISS | 无 | 完成：Qdrant dense+sparse + Agentic Retrieval |
| 在线知识图谱检索 | 无：仅构建/展示 | 无 | 完成：Neo4j、实体解析、路径、对比、证据子图 |
| 图谱事实治理 | 无 | 无 | 完成：candidate、review、resolution、撤下 |
| 多模态知识 | 部分 | 文档文本提取 | 图片/PDF Vision、区域证据、OCR 文本层 |
| 长期记忆 | 摘要 + 规则画像 | 日归档 + 结构化对象 | 类型化 Memory + provenance + revoke + native audit |
| Skill 自进化 | 无 | 脚本 Skill，无评测 | Draft/replay/shadow/canary/active/rollback |
| Skill 在线生效 | 无 | 默认自动激活 | 完成：run 冻结 index + 精确版本激活工具 |
| 本机文件读取 | 无 | 读写改删 | 完成：安全只读、PDF/DOCX/XLSX、citable evidence |
| 任务/计划/笔记 | 完成 | Todo 仍在规划 | 完成：Task/Plan/Step/Checklist/Note + Hermes tools |
| 稳定人格/首次引导 | 无 | 完成 | 完成：Persona/Onboarding + frozen run capsule |
| 按天日记 | 每日笔记 | 完成 | 完成：Day Archive、第一人称 Diary、Calendar |
| 情绪状态 | 无 | 完成 | 完成：确定性 reducer、TTL override、style-only capsule |
| 后台任务可靠性 | 进程内 task | 内存 FIFO | Postgres lease/retry/checkpoint/outbox |
| 桌面分发 | PyQt 原型 | macOS/Windows Electron | 无；当前是 Docker/Web |
| 证据发布门禁 | 无 | 无 | 完成：run allowlist + strict publisher |
| 测试/类型/容器 | 原型级 | 没有 test/spec | pytest + strict mypy + Ruff + 5-service Compose |

### 4.1 只计算用户功能的复核

| 用户功能 | 来源项目 | HermesGraph 覆盖 | 结论 |
| --- | --- | --- | --- |
| 通用流式对话与工具调用 | 两者 | 完整 | Hermes 在线 Loop、SSE 和工具事件已具备 |
| 个人文档知识库与跨文档检索 | 两者仅部分 | 完整且更强 | PDF/Office/图片、Agentic RAG 和 528 篇公共语料 |
| 可查询知识图谱 | ChatTutor | 完整且更强 | ChatTutor 主要构建/展示；HermesGraph 可在线实体、路径和对比检索 |
| 长期记忆检索 | 两者 | 完整 | 类型化 Memory、检索、查看和撤回已具备 |
| 记忆纠错或遗忘 | Desktop-Claw | 完整 | 自然语言 forget/replace、多候选确认、revoke provenance 与 UI |
| Skill 渐进披露与按需激活 | Desktop-Claw | 完整且更强 | discovery、activation 和精确版本生效已具备 |
| 本机 PDF/DOCX/XLSX 读取 | Desktop-Claw | 完整 | Computer Workspace 支持安全只读与可引用证据 |
| 本机文件创建、修改、删除 | Desktop-Claw | 不包含 | 属于明确安全边界，不应为了“全包含”直接照搬 |
| 苏格拉底式 Tutor/Judge/Inquiry 教学 | ChatTutor | 不包含 | 当前是通用 Agent，没有教学角色和专用教学状态机 |
| Task CRUD 与任务工作区 | ChatTutor | 完整 | Task 状态、优先级、截止日期、完成/归档和行动中心已具备 |
| 交互式学习计划生成、确认、暂停、继续 | ChatTutor | 部分 | Plan/PlanStep 状态机和 UI 已具备；没有专用多轮教学计划生成器 |
| 每日笔记、任务笔记与计划 checklist | ChatTutor | 完整 | Note/Checklist 结构化后端、Hermes tools 与任务详情入口已具备 |
| 学习时间线、每日/任务总结和日历 | ChatTutor | 部分 | Day Archive、Diary、Calendar 已具备；没有教学时长时间线 |
| 目标、偏好、薄弱点、误区、进展画像 | ChatTutor | 部分 | Persona/Memory 可表达偏好；没有 Learner Profile 专用弱点/误区 schema |
| Web 控制台 | ChatTutor | 完整 | HermesGraph 已有 Agent 工作台 |
| PyQt/Electron 桌面常驻入口 | 两者 | 不包含 | 当前交付形态是 Docker + Web |
| 稳定 SOUL 人格和首次认识流程 | Desktop-Claw | 完整 | 专用 Persona、Onboarding、兴趣/边界和 runtime capsule 已具备 |
| 按天对话归档、第一人称日记与日历回顾 | Desktop-Claw | 完整 | Day Archive、第一人称 Diary、月历和可编辑回顾已具备 |
| 情绪状态与 Companion Profile | Desktop-Claw | 完整 | 7 状态 reducer、reason codes、手动 TTL override 和 Persona UI |
| 桌面拖放、快速输入和浮窗状态 | Desktop-Claw | 不包含 | Web 上传不能等同于系统级桌面入口 |
| 跨平台安装包 | Desktop-Claw | 不包含 | 当前没有 macOS/Windows 桌面分发 |
| 语音对话 | ChatTutor 桌宠代码 | 不包含 | ChatTutor 客户端调用 `/voice_chat`，但 `main` 后端没有对应路由；两边都不计为已交付功能 |

因此不能说 HermesGraph “完全包含两个项目”。更准确的功能结论是：

> HermesGraph 已覆盖两个项目的通用 Agent、知识、Memory、Task/Plan/Note、Persona、日归档和
> Emotion 能力；仍未包含 ChatTutor 的专用教学工作流，以及 Desktop-Claw 的系统级桌面外壳。

## 5. 本轮补齐

### 5.1 Computer Workspace Toolset v1

新增 `list_workspace_files`、`read_workspace_file`、`search_workspace_files`：

- 只访问显式 root alias，scope 固定到配置的 tenant/project。
- 阻断绝对路径、`..`、隐藏目录、凭据样式文件、私钥后缀和所有 symlink。
- 使用 `O_NOFOLLOW`、文件字节上限、PDF 页数、ZIP 解压总量、扫描文件数、输出字符数和 run
  调用预算。
- 使用标准库解析 DOCX/XLSX，使用 pypdf 解析 PDF，不需要模型 API。
- 读取和命中结果生成 `workspace_file` provenance 与本轮 `evidence_id`，继续经过严格发布门禁。
- 标准 Compose 只把仓库 `workspace/` 以只读方式挂载为 `workspace` root；不挂载 HOME。

### 5.2 Governed Skill 在线激活

- `RuntimeCapsuleProvider` 已接入 `HermesAgentRuntime`，每轮只注入当前 run 钉住版本的相关
  Memory 和 Skill discovery index。
- 新增 `activate_governed_skill`；服务端从 run snapshot 解析精确版本，只允许 Canary/Active，
  调用方不能换成另一版本。
- 返回的是声明式步骤、能力和约束，不执行脚本、不扩大工具权限，也不创建第二 Agent Loop。
- 激活事件进入 trajectory，Canary/Active health gate 可据此只统计真实激活样本。

### 5.3 Personal Control Plane

- 新增 Task/Plan/PlanStep/Checklist/Note、Persona/Onboarding、Day Archive/Diary/Calendar、
  自然语言 Memory 纠错和 Emotion reducer。
- JSON/Postgres 双持久层、Postgres v11、乐观并发、append-only personal event 已接入。
- 新增六个 Hermes personal tools，复用 run scope、预算和工具事件；personal context 在 run start
  冻结，Emotion 明确为 style-only。
- 工作台新增行动、回顾、个人三个入口，并在 Memory 页增加自然语言更正与候选确认。
- 完整合同见 `docs/PERSONAL_CONTROL_PLANE.md`。

## 6. 仍需补充的 API-free 项

1. **P0：公开 `/v1` API 的用户认证和 scope 授权。** 当前内部 Bridge 有 bearer token，但公共
   run、memory、Skill、graph review 和 document API 仍主要按本地单用户信任模型运行。
2. **P1：持久交互 Run 与 SSE cursor/resume。** 当前 durable ingestion/learning job 完整，
   用户对话 run 本身还不能在断线后按 event cursor 恢复。
3. **P1：把 DOCX/XLSX 从临时 workspace 读取提升到知识摄取。**
4. **P2：可选教学 DomainPack。** 若要完整覆盖 ChatTutor 的差异化体验，应新增
   Tutor/Judge/Inquiry 路由、学习时长和 Learner Profile，而不是污染通用 Persona。
5. **P2：可选桌面壳。** Electron 悬浮球、系统级拖放和跨平台安装是独立产品形态，不进入
   Agent 核心后端。

## 7. 面试表述

不要说“复刻了 ChatTutor/Desktop-Claw 的全部功能”。更可信的表述是：

> 我阅读并拆解了两个个人 Agent 项目。ChatTutor 的优势是教学任务产品化，Desktop-Claw 的优势是
> 桌面连续感；我的项目选择把工程重心放在 Agentic Retrieval、Neo4j GraphRAG、证据发布和可回滚
> 自进化。之后补齐了 Task/Plan/Note、Persona、Day Archive、自然语言 Memory 纠错和确定性
> Emotion，并将它们接入 Hermes 工具与 Postgres 控制面。仍未冒充完成的是专用教学状态机、
> Electron 桌面壳、公开 API 鉴权和持久 SSE 恢复。

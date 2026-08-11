# HermesGraph 项目目录结构

本文是仓库文件归属的维护契约。目录按运行责任划分，不按临时功能堆叠。整理时保留所有运行路径、
迁移、可复现实验、企业样例和本地知识资产；缓存、锁文件、编译副产物和已经退出运行时的兼容代码
不属于项目资产。

## 1. 顶层结构

```text
.
├── app/                 Python 产品源码
├── deploy/hermes/       Hermes sidecar、插件和 toolset 部署代码
├── frontend/            React/Vite 工作台
├── tests/               unit、contract、integration 测试
├── examples/            企业知识库、黄金集和可复现样例
├── assets/              离线运行依赖的受控静态资产
├── prompts/             真正由运行时加载的顶层 prompt
├── scripts/             运维、数据生成和 live smoke 入口
├── docs/                PRD、技术设计、ADR、进度与使用文档
├── typings/             第三方库的本地 mypy stub
├── workspace/           Compose 挂载的只读用户文件入口
├── .data/               本地语料、OCR/IR、索引清单和运行状态
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── requirements*.lock
```

`.data/` 中的论文、OCR 结果、Document IR、知识库和回填 manifest 是当前项目的工作资产，不能当作
缓存批量删除。只有其中的 `*.lock` 是进程协调副产物。`.venv/`、`frontend/node_modules/` 和
`frontend/dist/` 可重建，但本地开发期间可以保留；它们不进入版本控制。

## 2. 后端源码边界

| 目录 | 单一职责 |
| --- | --- |
| `app/agent/` | Adaptive-RAG、Context Engine、Hermes runtime、工作区工具和答案发布 |
| `app/api/` | FastAPI 路由、鉴权、请求响应 schema 和 SSE 接口 |
| `app/application/` | Run/Workspace 用例编排、SSE 生命周期和运行事件记录 |
| `app/domain/` | 跨模块共享的纯模型、枚举和端口合同 |
| `app/domain_packs/` | 可插拔领域包；与 `domain/` 的核心合同职责不同 |
| `app/retrieval/` | Dense/Sparse/BM25 混合召回、Qdrant 适配和检索控制器 |
| `app/graph/` | 图谱抽取、候选治理、Neo4j 投影、维护与 GraphRAG 工具 |
| `app/knowledge/` | Document IR、解析、token-aware chunk、摄取和重建索引 |
| `app/learning/` | 回合反思、Memory/Skill 候选、评估、晋升与后台任务 |
| `app/harness/` | MemoHarness 固定化经验、诊断、pattern 治理和回填 |
| `app/memory/` | 项目记忆存储、写入门禁和安全 prompt 编译 |
| `app/skills/` | governed skill registry、版本选择和激活 |
| `app/personal/` | Task、Plan、Note、Persona、Emotion 和日归档控制面 |
| `app/web_search/` | 外部网页检索 provider 与 live contract |
| `app/sources/` | arXiv 等离线知识源同步和 OCR 入口 |
| `app/infra/` | Postgres 等持久化实现与 schema migration |
| `app/capabilities/` | Capability Registry、LangChain 桥接和适配器 |
| `app/evaluation/` | 离线/在线黄金集评测器和 CLI |
| `app/demo/` | 可重复演示数据，不承载生产逻辑 |

`app/bootstrap.py` 是依赖装配根，`app/main.py` 是 ASGI 入口，`app/cli.py` 是本地 CLI，
`app/worker.py` 是后台任务入口。新模块只有在形成稳定责任边界时才新建顶层包；单个 helper 应放在
现有 owner 包中。

### 2.1 在线请求主调用链

维护在线问答时按下面顺序阅读，不需要在目录之间猜入口：

```text
api/app.py
  -> application/run_stream.py + run_service.py
  -> agent/adaptive_rag_router.py
  -> agent/context_engine.py
  -> agent/hermes_runtime.py + hermes_bridge.py
  -> capabilities/agent_tool_runtime.py
  -> retrieval/agentic_retrieval.py
  -> retrieval/hybrid_retrieval_pipeline.py
       -> qdrant_hybrid_retriever.py
       -> graph/graph_retrieval_tools.py
  -> agent/answer_publisher.py
```

知识入库走另一条独立链路：

```text
knowledge/knowledge_ingestion.py
  -> document_ir.py
  -> hierarchical_chunking.py
  -> knowledge_repository.py
  -> retrieval/qdrant_hybrid_retriever.py
  -> graph/graph_candidate_service.py
  -> graph/neo4j_evidence_graph.py
```

## 3. 数据、样例与测试

- `examples/enterprise_knowledge/`：虚构但结构完整的研发企业知识库，用于产品体验和检索验收。
- `examples/evaluation/`：版本化黄金集及其冻结视觉资产。`vision_golden_v1-v3.json` 是兼容性测试
  输入，不是重复备份。
- `examples/knowledge/`：最小知识摄取样例。
- `.data/arxiv/`：已下载论文及 OCR/IR，作为个人补充知识层保留。
- `tests/unit/`：纯逻辑和内存适配器；`tests/contract/`：跨实现合同；`tests/integration/`：真实组件
  协作边界。测试 fixture 不应放进 `app/`。

## 4. 文件放置规则

1. 产品逻辑进入拥有该能力的 `app/<bounded-context>/`，API 层只做协议转换。
2. 数据库实现进入 `app/infra/`；领域层只能依赖端口，不能反向导入 FastAPI 或数据库客户端。
3. 一次性但需要复现的生成器和 live smoke 放 `scripts/`，并登记在 `scripts/README.md`。
4. 正式可安装命令优先登记在 `pyproject.toml [project.scripts]`，不要再增加只有 `main()` 转发的包装脚本。
5. 运行时 prompt 只有被代码加载时才放 `prompts/`；模块私有结构化 prompt 与实现放在同一 Python 模块。
6. 架构决策使用唯一递增 ADR 编号；企业样例内的 ADR 属于 fixture 自己的命名空间。
7. 不提交 `.DS_Store`、`__pycache__`、工具缓存、`dist/`、`*.tsbuildinfo`、生成后的 Vite 配置或
   `.data/**/*.lock`。

## 5. 本次审计结论

- `app/` 的全部 Python 模块都有入口、入站引用或显式 CLI，未删除任何业务包。
- 前端全部视图由静态或 `React.lazy()` 动态入口使用，均予保留。
- 删除已退出生产路径的正则社交路由；Adaptive-RAG 模型决策是唯一在线路由实现。
- 删除未加载的 reflection/skill-miner Markdown prompt；有效合同已由相应结构化实现和测试持有。
- 删除只转发正式模块 CLI 的脚本，保留所有有独立诊断、生成或运维行为的脚本。
- 修正重复 ADR-010：Hermes 生命周期保留 ADR-010，Semantic GraphRAG 调整为 ADR-012。
- 架构级收敛后，顶层业务包从 24 个降为 19 个；单文件的
  context/evidence/observability/computer/vision 已归入实际 owner，内置 domain pack 从 5 个文件
  合并为 `domain_packs/built_in.py`。
- 核心模块使用完整职责命名，例如 `adaptive_rag_router.py`、`agent_tool_runtime.py`、
  `hybrid_retrieval_pipeline.py`、`qdrant_hybrid_retriever.py`、`neo4j_evidence_graph.py` 和
  `knowledge_repository.py`，不再使用脱离目录后语义模糊的 `runtime.py`、`pipeline.py`、`local.py`。
- `app/tokenization.py` 是 chunking 与 Context Engine 共用的唯一 tokenizer 入口，优先读取仓库内置
  `o200k_base` 资产，不在运行时访问公网下载编码文件。

以后进行清理时，应先运行引用扫描、Ruff、mypy、pytest 和前端 typecheck/build；不能仅凭文件名或
文件大小删除知识资产、迁移、黄金集和历史兼容 fixture。

# Scripts

这里仅保留有独立行为、需要人工运行或用于复现实验的脚本。正式产品 CLI 在 `pyproject.toml` 的
`[project.scripts]` 中维护。

| 脚本 | 用途 |
| --- | --- |
| `docker_up.sh` | 校验环境并启动完整 Compose 栈 |
| `run_kg_extraction.sh` | 以前台进度条并发执行 KG 回填 |
| `run_kg_extraction_background.sh` | 后台执行完整 KG 回填并记录 PID/日志 |
| `check_learning_reflection.py` | 对当前 provider 执行一次结构化 reflection live smoke |
| `check_learning_workers.py` | 验证 Postgres learning job worker 的竞争与终态 |
| `infrastructure_smoke.py` | 验证真实 Qdrant 与 Neo4j 适配器 |
| `generate_vision_eval_assets.py` | 可重复生成视觉黄金集的合成图片 |

arXiv 同步和 Web Search 检查直接使用正式入口：

```bash
./.venv/bin/python -m app.sources.arxiv_cli --help
./.venv/bin/python -m app.web_search.cli --help
```

企业 fixture 导入与企业 RAG 评测同样是正式 CLI，分别由
`hermesgraph-enterprise-fixture` 和 `hermesgraph-eval-enterprise` 注册；源码 checkout 中也可直接
使用文档记录的 `python -m app.demo.enterprise_fixture_cli` 与
`python -m app.evaluation.enterprise_cli`。

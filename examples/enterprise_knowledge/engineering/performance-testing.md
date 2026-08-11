# Atlas 性能测试方法

状态：Active
版本：2026.08
Owner：Performance Engineering

## 场景矩阵

性能测试分别测 conversation fast lane、单次 RAG、多轮 agentic retrieval、四跳图谱、带图片上传和批量
摄取。每类报告吞吐、P50/P95/P99、首可见阶段、首 token、工具时间、错误率和资源使用，禁止只给平均值。

## 数据

使用合成企业资料和公开技术论文，保持真实文件大小、标题层级、Chunk 分布和多语言比例。至少包含 100
个 workspace，每个 10 到 10,000 份文档，验证 Qdrant payload filter 不随全局数据线性退化。压测数据
不得含生产客户正文。

## 方法

先在固定资源下找单实例饱和点，再做水平扩展。预热连接池和模型通道，但检索查询包含稳定随机种子，
避免全部命中缓存。测试持续 30 分钟稳态并执行 10 分钟 2 倍突发；同时注入 5% provider 429、Qdrant
200 ms 延迟和一次 PostgreSQL leader failover。

## 预算

conversation P95 2 秒；Polaris direct lookup P95 800 ms；两跳 Constellation P95 500 ms；首次 ingestion
状态 300 ms；10 MB PDF 在非 OCR 情况 60 秒内 ready。错误率小于 1%，scope 或引用错误必须为 0。

## 结论

性能回归必须定位到内部服务、provider 或队列，不把 provider 长尾归因于 Gatehouse。任何优化都同时
检查答案质量、引用覆盖和取消响应，不能用减少证据换取虚假低延迟。

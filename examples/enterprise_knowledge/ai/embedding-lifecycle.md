# Atlas Embedding 生命周期管理

状态：Active
版本：2026.08
Owner：Knowledge Systems

## 版本标识

Embedding revision 由 provider、model、维度、归一化方式、分块 revision 和预处理哈希共同确定。不同
revision 的向量不能写入同一 named vector，也不能用旧评测结果批准新空间。当前生产 dense 维度为
1536，向量执行 cosine normalization。

## 发布流程

新模型先对固定企业集和公共技术集离线回填，比较 Recall@10、MRR、hard-negative 命中和跨语言查询。
required case 必须 100% 通过，MRR 不得比当前版本下降超过 0.02。随后执行 10% 影子查询，观察 top-k
重叠、检索延迟和无结果率。

通过后 Foundry 对新写入双写旧、新 named vector；后台按 document revision 回填存量。回填进度达到
100%、随机 hash 对账通过后，Polaris 才切换 Qdrant alias。旧 collection 保留 7 天用于秒级回滚。

## 失败处理

部分文档 embedding 失败时 Job 保持 indexing 或 partial，不能把零向量写入。429 和 timeout 可重试；
输入过长必须回到 Chunker 修复，不能静默截断。模型输出维度变化立即失败预检。

## 漂移监控

每周采样真实匿名查询，监控 query/document norm、语言分布、top-k 相似度和来源多样性。漂移只触发评估，
不会自动更换模型。任何重新 embedding 都必须保留 source revision 和 content hash 可追溯性。

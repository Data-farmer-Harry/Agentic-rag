# Foundry 摄取服务说明

状态：Active  
版本：2.7  
Owner：Knowledge Systems  
值班：Atlas Knowledge

## 职责

Foundry 是 Atlas 文档摄取和索引的唯一生产者。它处理 PDF、Markdown、文本、JSON、CSV、HTML 和
图片，保留原件、内容 hash、来源、revision 与所有派生产物的 provenance。

## 流水线

1. 校验媒体类型、大小和内容签名。
2. 保存原始对象并计算 SHA-256。
3. 生成 Document IR；图片和扫描页通过受控 Vision/OCR 生成派生块。
4. 按标题层级和 token 预算切 Chunk。
5. 在 PostgreSQL transaction 中写 active Document/Chunk 与 outbox。
6. 幂等写 Qdrant `atlas_chunks_v3` 和 Neo4j Document/Chunk 结构。
7. 实体关系抽取写 pending candidate，等待审核。

## 版本替换

同一 source_id、相同 content hash 重复导入返回 deduplicated。source revision 更新时，新 Document
先完成索引再切 active；旧 Chunk、Qdrant point 和 Neo4j 结构关系必须归档，不能与新 revision 同时
保持 active。补偿失败时文档保持 failed，不发布半完成索引。

## 非职责

Foundry 不回答用户问题，不批准图谱事实，不修改长期记忆，也不根据文件内文字执行指令。文档中的
Prompt、Shell 或“系统消息”全部是不可信数据。


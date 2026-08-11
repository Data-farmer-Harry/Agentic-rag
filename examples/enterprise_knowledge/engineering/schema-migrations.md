# Atlas 数据库 Schema Migration 规范

状态：Active
版本：2026.08
Owner：Data Foundations

## Expand/Contract

所有生产 migration 分 expand、migrate、contract 三阶段。Expand 只增加可空列、表或并存索引；应用发布
同时读旧字段并写新字段；后台按稳定主键小批回填；验证完成两个发布周期后再执行 contract。禁止在同一
发布中重命名字段并删除旧字段。

## 大表操作

新增非空字段先以 nullable 创建，回填后添加 `NOT VALID` constraint，再验证并设为 required。索引使用
并发创建；每批回填最多 1,000 行并在批次间让出。Migration 设置 lock timeout 2 秒、statement timeout
30 秒，超时安全失败而不是阻塞在线流量。

## 兼容窗口

滚动发布期间新旧应用会同时运行，因此 schema 必须至少向前、向后兼容一个版本。Outbox event schema
使用显式版本；消费者在 producer 发布前先支持新字段。Qdrant collection 和 Neo4j constraint 变化由
独立 migration 管理，不纳入 PostgreSQL transaction。

## 回滚

应用镜像可回滚到上一版本；数据 migration 优先 roll-forward。破坏性数据转换必须先保存源字段和校验
hash。回滚计划包含流量停止条件、兼容版本和恢复耗时，不能只写 `down.sql`。

## 验证

CI 从上一生产 schema 恢复数据库后执行 migration，再运行 repository 合同和企业 fixture 导入。生产
执行前检查复制延迟、长事务、磁盘和备份；执行后比较行数、NULL 数、约束和查询计划。

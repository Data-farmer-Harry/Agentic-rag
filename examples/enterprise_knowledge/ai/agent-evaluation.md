# Atlas Agent 评测体系

状态：Active
版本：2026.08
Owner：AI Quality

## 三层评测

检索层测 Recall@K、MRR、forbidden source 和 scope isolation；图谱层测实体解析、路径关系、证据覆盖和
越权路径；回答层测 required facts、forbidden claims、引用准确性、拒答、工具预算和最终 Answer schema。
任何一层失败都不能被后续模型“看起来合理”的文字掩盖。

## 数据集

企业 required set 覆盖架构多跳、服务依赖、事故根因、当前/历史 ADR、Runbook 顺序、提示注入和未知
组件。每个 case 固定 required sources、facts、forbidden claims、最少引用和期望图路径。数据集与
manifest revision 一起版本化。

## 发布门禁

required case 必须 100% 通过；检索 Recall@10 至少 0.90、MRR 至少 0.75；引用必须能映射到 active
Chunk；未知组件必须 insufficient。性能按 lane 统计，conversation P95 小于 2 秒，直接知识查询 P95
目标 15 秒，多跳综合 P95 目标 30 秒。

## 线上观察

生产只记录脱敏问题类别、工具轨迹摘要、证据 ID、延迟和反馈，不保存 provider chain-of-thought。
负反馈进入人工复核池，用于新增 regression case，而不是直接作为模型微调事实。

## 防止评测污染

评测答案不能进入生产知识库或长期 Memory。Judge 模型版本、Prompt 和输入哈希必须记录；模型 Judge
只作为辅助，required facts 和 forbidden claims 仍采用确定性检查。报告必须区分 offline fixture、live
retrieval 和 live answer，不能混成一个分数。

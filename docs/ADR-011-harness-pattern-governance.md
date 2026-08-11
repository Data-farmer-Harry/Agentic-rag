# ADR-011: Governed Harness Pattern Evaluation and Consumption

状态：Accepted

日期：2026-07-30

## Context

HermesGraph 已有不可变 Experience Bank、D1-D6 诊断、Pattern Draft miner 和 run-scoped
observe/shadow overlay，但 Pattern 定义中的 `status` 不能被原地覆写，否则旧运行无法重放，晋级
理由也会随 evaluator 升级而漂移。同时，直接把 `HarnessConfigDelta` 传给 Hermes 会形成第二套
隐式 Agent policy，并可能绕过部署预算和 scope。

## Decision

1. `HarnessPattern` 继续是不可变定义；有效状态由 append-only transition ledger 推导。
2. Pattern 治理拆成三个不可变资产：
   - `HarnessPatternEvaluation`：逐 supporting/contradicting/required case 的投影结果；
   - `HarnessPatternPromotionEvidence`：冻结 dataset revision、阈值、evaluation hash 和晋级结论；
   - `HarnessPatternTransition`：记录 from/to、allowed/applied、人工批准和精确 evidence hash。
3. 状态机固定为
   `Draft -> OfflinePass -> Shadow -> Canary -> Active -> Deprecated`，禁止跳级。
4. Draft 到 Shadow 可在全部 required case、scope/hash、evidence integrity 和 non-regression
   门禁通过后自动完成；Canary 和 Active 必须人工批准。
5. PostgreSQL v14 为 Evaluation、Promotion Evidence 和 Transition 建独立表。应用 transition
   在 pattern 行锁内校验当前有效状态，并以 applied-from 唯一索引阻止并发双晋级。
6. `RunExecutionPolicy` 是唯一在线消费合同。它在 run start 解析、clamp、hash 并同时冻结到
   `RunContext` 与 `RunSnapshot`，运行中不可重算。
7. 第一版只消费具有唯一执行语义的低风险字段：
   - `context.capsule_memory_limit`
   - `memory.memory_min_confidence`
   - `orchestration.retrieval_profile`
   - `orchestration.max_subqueries`
   - `orchestration.max_retrieval_rounds`
   - `tool.graph_hops`
8. generation/output、token/字符预算、private quota、source diversity 和 graph follow-up 等字段
   仍可离线评估和 Shadow 观察，但在语义统一前不得进入 Canary。
9. Observe/Shadow 永远设置 `behavior_applied=false`。Canary 使用
   `SHA-256(revision, scope, run_id, pattern@version)` 稳定分桶；Active 对匹配版本全量应用。
10. Pattern bank、ledger、projection 或 hash 异常时 fail closed 到 baseline。Overlay 不能提高
    总工具预算、分类预算、timeout、权限、scope 或证据发布权限。

## Consequences

- 历史运行可依赖 Snapshot 中的 exact policy/hash 重放，不依赖 Pattern Bank 当前状态。
- Required case 失败不能被平均质量分掩盖。
- 同一 Pattern version rollback 后不能重新激活；修复必须产生新版本。
- Capsule、retrieval controller、AgentToolRuntime 和 Hermes Bridge 只读取冻结策略，不拥有
  学习或晋级逻辑。
- 当前完成的是可治理的离线晋级和有界消费；Canary health 聚合与自动 rollback 属于 MH-015。

## Verification

- 324 tests collected，307 passed，17 个环境型 skip。
- 16/16 真实 PostgreSQL adapter contract 通过。
- Ruff 全绿，162 个应用源码 strict mypy 通过。
- Docker 中 migration `14:harness_pattern_governance` 与三张治理表已验证。
- Observe 模式真实问候运行 `behavior_applied=false`、零工具调用，约 45 ms 完成。

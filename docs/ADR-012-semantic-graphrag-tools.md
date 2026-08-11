# ADR-012: Semantic GraphRAG tools over generated Cypher

- Status: accepted
- Date: 2026-07-22

## Context

Hermes needs to decide when graph structure can answer an entity, comparison, multi-hop, or conflict question.
Giving the model a free-Cypher tool would also give it control over traversal depth, labels, relationship types,
scope predicates, result size, and evidence joins. Prompt instructions cannot reliably enforce those database and
authorization invariants.

The existing Neo4j adapter already owns three fixed traversal templates (`neighbors`, `paths`, `conflicts`) and
rejects cross-scope or unbacked paths. It lacked semantic operations that an Agent can select without understanding
storage details.

## Decision

Expose four read-only tools through the Hermes bridge:

1. `resolve_graph_entities`: deterministic canonical-name/alias/type resolution with source evidence.
2. `retrieve_evidence_subgraph`: parallel entity and text retrieval followed by bounded 1-3 hop expansion and
   evidence fusion.
3. `compare_graph_entities`: deterministic connecting-path and shared/exclusive-neighbor analysis.
4. `search_graph`: low-level traversal through the existing allowlisted templates.

The tools receive scope only from `RunContext`. `GraphRetrievalToolkit` composes typed ports inside the LangChain
Integration Runtime. Capability Registry enforces JSON schema, scopes, timeout, output bytes, and provenance policy;
the Hermes bridge adds total/per-graph budgets, duplicate-call detection, event audit, and a run-local evidence
allowlist. Evidence fusion prefers stable `chunk_id` identity across Qdrant and Neo4j, then falls back to
provenance identity. Neither the Agent nor an API caller can supply Cypher.

## Consequences

- Graph retrieval is explainable as resolved entities, concrete paths, topology sets, evidence, and decision trace.
- Neo4j and the deterministic in-memory backend share the same domain contracts and contract tests.
- Adding a new traversal requires reviewed code and a fixed query template instead of a prompt change.
- The first resolver uses deterministic lexical alias matching. Embedding-based entity linking or learned reranking
  may be added behind the same port only after a versioned evaluation gate.
- Graph reads remain bounded to three hops. Larger community or global graph algorithms belong in offline derived
  indexes, not in the online Agent tool.

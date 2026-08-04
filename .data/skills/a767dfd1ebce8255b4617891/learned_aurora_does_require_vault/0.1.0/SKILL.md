---
skill_id: f61b0bb1-1bc3-52af-b959-4d5d041b7a6d
tenant_id: local
project_id: default
name: learned_aurora_does_require_vault
version: 0.1.0
description: Execute a stable 1-step workflow mined from 4 similar trajectories.
status: draft
trigger:
  intents:
  - aurora
  - does
  - require
  - vault
  - what
  phrases:
  - What does AURORA-VAULT-8301 require?
steps:
- action: search_knowledge
  purpose: Execute approved action search_knowledge
  inputs: {}
allowed_capabilities:
- search_knowledge
constraints:
  max_tool_calls: 1
  mined_from_repeated_runs: 4
  average_sequence_distance: 0.0
  no_arbitrary_code: true
  requires_evidence_validation: true
source_run_ids:
- 2628c92b-6c54-4643-bb99-bd8d8916f29a
- a7436cd5-6b5c-4823-97c4-fcf552b5f30a
- ca24c58f-0726-4340-b4fe-916d3c615a31
created_at: '2026-07-15T02:35:13.520177+00:00'
---

# learned_aurora_does_require_vault

Execute a stable 1-step workflow mined from 4 similar trajectories.

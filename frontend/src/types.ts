export type EvidenceLevel =
  | "verified"
  | "supported"
  | "inferred"
  | "insufficient"
  | "conflicting";
export type AnswerMode = "grounded" | "conversational" | "action";
export type RoutingLane = "deterministic" | "conversation" | "agent";

export interface Provenance {
  source_type: string;
  source_id: string;
  content_hash?: string;
  trust: string;
  locator?: Record<string, unknown>;
  observed_at: string;
}

export interface Evidence {
  evidence_id: string;
  text: string;
  title?: string;
  score: number;
  provenance: Provenance;
  metadata: Record<string, unknown>;
}

export interface Claim {
  text: string;
  evidence_ids: string[];
  level: EvidenceLevel;
}

export interface Answer {
  answer_markdown: string;
  response_mode: AnswerMode;
  routing_lane?: RoutingLane;
  claims: Claim[];
  citations: Evidence[];
  /** Optional until the answer view model exposes evidence-backed graph paths. */
  graph_paths?: GraphPath[];
  confidence: EvidenceLevel;
  limitations: string[];
  followup_queries: string[];
  follow_up_actions: FollowUpAction[];
}

/** Payload sent for `run.completed` and persisted with a completed run. */
export interface RunCompletedEvent {
  run_id: string;
  status: string;
  answer: Answer;
  tool_events: ToolEvent[];
  learning_change_count: number;
  duration_ms: number;
}

/** Deterministic, read-only next-query projection from AnswerResponse. */
export interface FollowUpAction {
  action_id: string;
  kind: "query";
  label: string;
  query: string;
}

export interface ToolEvent {
  tool_name: string;
  input_hash: string;
  output_summary: string;
  detail: Record<string, unknown>;
  success: boolean;
  duration_ms: number;
  created_at: string;
}

export interface RunContext {
  run_id: string;
  tenant_id: string;
  project_id: string;
  user_id: string;
  session_id: string;
  domain_pack: string;
  model: string;
  skill_versions: Record<string, string>;
  started_at: string;
}

export interface RunTrajectory {
  context: RunContext;
  user_input: string;
  idempotency_key?: string;
  status: string;
  answer?: Answer;
  tool_events: ToolEvent[];
  feedback_score?: number;
  tags: string[];
  completed_at?: string;
  snapshot?: {
    model: string;
    domain_pack: string;
    domain_pack_version: string;
    skill_versions: Record<string, string>;
    config_hash: string;
  };
}

export interface MemoryRecord {
  memory_id: string;
  memory_type: string;
  key: string;
  summary: string;
  detail: Record<string, unknown>;
  confidence: number;
  provenance: Provenance[];
  created_at: string;
  updated_at: string;
  revoked_at?: string;
}

export interface SkillStep {
  action: string;
  purpose: string;
  inputs: Record<string, unknown>;
}

export interface SkillDefinition {
  skill_id: string;
  name: string;
  version: string;
  description: string;
  status: string;
  trigger_intents: string[];
  trigger_phrases: string[];
  steps: SkillStep[];
  allowed_capabilities: string[];
  source_run_ids: string[];
  created_at: string;
}

export interface SkillEvaluationCase {
  run_id: string;
  baseline_score: number;
  candidate_score: number;
  sequence_similarity: number;
  tool_success_rate: number;
  unsupported_claim_rate: number;
  passed: boolean;
  reasons: string[];
}

export interface SkillEvaluation {
  evaluation_id: string;
  skill_id: string;
  skill_version: string;
  evaluator_revision: string;
  baseline_score: number;
  candidate_score: number;
  unsupported_claim_rate: number;
  security_passed: boolean;
  regression_passed: boolean;
  case_count: number;
  passed_cases: number;
  cases: SkillEvaluationCase[];
  notes: string[];
  generated_at: string;
}

export interface SkillHealthReport {
  skill_id: string;
  skill_version: string;
  cohort: "shadow" | "canary" | "active";
  required_observations: number;
  total_observations: number;
  evaluated_observations: number;
  exposed_observations: number;
  activated_observations: number;
  average_baseline_score: number;
  average_candidate_score: number;
  average_unsupported_claim_rate: number;
  failure_rate: number;
  healthy: boolean;
  promotion_ready: boolean;
  reasons: string[];
  promotion_evidence: SkillPromotionEvidence;
}

export interface SkillPromotionEvidence {
  evidence_id: string;
  cohort: "shadow" | "canary" | "active";
  observation_ids: string[];
  run_ids: string[];
  required_observations: number;
  evaluated_observations: number;
  average_baseline_score: number;
  average_candidate_score: number;
  average_unsupported_claim_rate: number;
  failure_rate: number;
  negative_feedback_count: number;
  negative_feedback_rate: number;
  severe_negative_feedback_count: number;
  healthy: boolean;
  promotion_ready: boolean;
  recommended_action: "promote" | "hold" | "rollback_recommended" | "rollback";
  reasons: string[];
  generated_at: string;
}

export interface SkillEvolutionSnapshot {
  skill: SkillDefinition;
  latest_evaluation?: SkillEvaluation;
  health?: SkillHealthReport;
}

export interface SkillEvolutionResult {
  skill: SkillDefinition;
  evaluation: SkillEvaluation;
  transitions: PromotionDecision[];
}

export interface PromotionDecision {
  transition_id?: string;
  promotion_evidence_id?: string;
  skill_id: string;
  from_status: string;
  to_status: string;
  allowed: boolean;
  reasons: string[];
}

export interface LearningChange {
  change_set_id: string;
  target_type: string;
  target_id: string;
  structured_diff: Record<string, unknown>;
  source_run_ids: string[];
  expected_benefits: string[];
  risks: string[];
  evaluation_report: Record<string, unknown>;
  rollback_conditions: string[];
  created_at: string;
}

export interface KnowledgeDocument {
  document_id: string;
  tenant_id: string;
  project_id: string;
  user_id: string;
  filename: string;
  title: string;
  media_type: string;
  byte_size: number;
  content_hash: string;
  storage_key: string;
  status: "active" | "archived" | "failed";
  chunk_count: number;
  parser_version: string;
  error?: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface IngestionResult {
  document: KnowledgeDocument;
  deduplicated: boolean;
}

export type IngestionJobStatus =
  | "queued"
  | "running"
  | "retry_scheduled"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface IngestionJob {
  job_id: string;
  tenant_id: string;
  project_id: string;
  user_id: string;
  filename: string;
  media_type?: string;
  byte_size: number;
  content_hash: string;
  status: IngestionJobStatus;
  attempt: number;
  max_attempts: number;
  available_at: string;
  lease_expires_at?: string;
  document_id?: string;
  deduplicated?: boolean;
  can_retry: boolean;
  error_code?: string;
  error_message?: string;
  created_at: string;
  started_at?: string;
  completed_at?: string;
  updated_at: string;
}

export interface IngestionJobSubmission {
  job: IngestionJob;
  coalesced: boolean;
}

export interface Overview {
  runtime_mode: string;
  model: string;
  conversation_fast_path_model: string;
  conversation_history_turns: number;
  model_provider: string;
  learning_mode: string;
  learning_reflector_mode: string;
  retrieval_backend: string;
  embedding_provider: string;
  graph_backend: string;
  graph_extractor_mode: string;
  knowledge_repository_backend: "local" | "postgres";
  ingestion_mode: "sync" | "async";
  counts: {
    runs: number;
    memories: number;
    skills: number;
    active_skills: number;
    change_sets: number;
    documents: number;
    chunks: number;
    graph_entity_candidates: number;
    graph_relation_candidates: number;
    graph_resolution_candidates: number;
    pending_graph_candidates: number;
    ingestion_jobs: number;
    active_ingestion_jobs: number;
    outbox_unpublished: number;
  };
  domain_packs: string[];
  routing_lane_counts: Record<RoutingLane | "legacy", number>;
  capabilities: string[];
  workspace_profile?: WorkspaceProfile | null;
}

export type WorkspaceMode = "team" | "personal";
export type KnowledgeLayer = "team_internal" | "personal" | "public_reference";

/** Stable workspace defaults returned by the workspace overview contract. */
export interface WorkspaceProfile {
  tenant_id: string;
  project_id: string;
  display_name: string;
  workspace_mode: WorkspaceMode;
  enabled_knowledge_layers: KnowledgeLayer[];
  default_domain_pack: string;
  created_at: string;
  updated_at: string;
  version: number;
}

export type SampleWorkspaceImportStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

export interface SampleWorkspaceImport {
  run_id: string;
  fixture_id: string;
  manifest_revision: string;
  tenant_id: string;
  project_id: string;
  requested_by: string;
  dry_run: boolean;
  status: SampleWorkspaceImportStatus;
  plan: Array<{
    source_id: string;
    filename: string;
    action: "create" | "refresh" | "replace" | "historical" | "unchanged";
  }>;
  job_ids: Record<string, string>;
  completed_document_ids: Record<string, string>;
  archived_predecessor_ids: string[];
  errors: Record<string, string>;
  curated_graph_entities: number;
  curated_graph_relations: number;
  created_at: string;
  updated_at: string;
  completed_at?: string;
}

export interface GraphNode {
  node_id: string;
  label: string;
  name: string;
  properties: Record<string, unknown>;
}

export interface GraphRelationship {
  relationship_id: string;
  relation_type: string;
  source_node_id: string;
  target_node_id: string;
}

export interface GraphPath {
  nodes: GraphNode[];
  relationships: GraphRelationship[];
  evidence: Evidence[];
}

export interface GraphResult {
  paths: GraphPath[];
  evidence: Evidence[];
  trace: Record<string, unknown>;
}

export type GraphCandidateStatus = "pending" | "approved" | "rejected" | "archived";

export interface GraphEntityCandidate {
  candidate_id: string;
  document_id: string;
  canonical_name: string;
  entity_type: string;
  aliases: string[];
  source_chunk_ids: string[];
  confidence: number;
  extractor_revision: string;
  status: GraphCandidateStatus;
  rationale: string;
  reviewed_by?: string;
  reviewed_at?: string;
  created_at: string;
  updated_at: string;
}

export interface GraphRelationCandidate {
  candidate_id: string;
  document_id: string;
  source_candidate_id: string;
  target_candidate_id: string;
  source_name: string;
  target_name: string;
  relation_type: string;
  source_chunk_ids: string[];
  confidence: number;
  extractor_revision: string;
  status: GraphCandidateStatus;
  rationale: string;
  reviewed_by?: string;
  reviewed_at?: string;
  created_at: string;
  updated_at: string;
}

export interface EntityResolutionCandidate {
  candidate_id: string;
  left_entity_id: string;
  right_entity_id: string;
  left_document_id: string;
  right_document_id: string;
  left_name: string;
  right_name: string;
  canonical_name: string;
  entity_type: string;
  match_strategy: "exact_identifier" | "exact_name" | "normalized_name" | "alias_overlap";
  source_chunk_ids: string[];
  confidence: number;
  resolver_revision: string;
  status: GraphCandidateStatus;
  rationale: string;
  reviewed_by?: string;
  reviewed_at?: string;
  created_at: string;
  updated_at: string;
}

export interface GraphCandidateCollection {
  entities: GraphEntityCandidate[];
  relations: GraphRelationCandidate[];
  resolutions: EntityResolutionCandidate[];
}

export type StreamEventName =
  | "run.accepted"
  | "run.status"
  | "run.heartbeat"
  | "tool.completed"
  | "answer.delta"
  | "evidence.added"
  | "learning.updated"
  | "run.completed"
  | "run.cancelled"
  | "run.error";

export interface RunStartResponse {
  run_id: string;
  status: "running" | "completed" | "failed" | "cancelled";
  idempotency_key: string;
  coalesced: boolean;
}

export interface ConversationMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: Date;
  runId?: string;
  status?: string;
  phase?: "accepted" | "understanding" | "executing" | "synthesizing" | "completed";
  confidence?: EvidenceLevel;
  responseMode?: AnswerMode;
  citations?: Evidence[];
  graphPaths?: GraphPath[];
  followUpActions?: FollowUpAction[];
  toolEvents?: ToolEvent[];
  limitations?: string[];
  durationMs?: number;
  learningCount?: number;
  feedbackScore?: number;
  streaming?: boolean;
  error?: string;
  errorCode?: string;
  retryable?: boolean;
  cancelled?: boolean;
  attachments?: string[];
}

export interface ConversationSummary {
  session_id: string;
  title: string;
  preview: string;
  run_count: number;
  last_run_id: string;
  last_status: string;
  domain_pack: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface ConversationMetadata {
  tenant_id: string;
  project_id: string;
  user_id: string;
  session_id: string;
  title?: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export type TaskStatus =
  | "inbox"
  | "planned"
  | "in_progress"
  | "blocked"
  | "completed"
  | "archived";

export interface PersonalTask {
  task_id: string;
  title: string;
  description: string;
  status: TaskStatus;
  priority: number;
  due_at?: string;
  tags: string[];
  completed_at?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export type ReminderKind = "overdue" | "due_soon" | "today";

export interface TaskReminder {
  task_id: string;
  title: string;
  kind: ReminderKind;
  due_at: string;
  priority: number;
  unread: boolean;
  snoozed_until?: string;
}

export interface TaskReminderFeed {
  items: TaskReminder[];
  unread_count: number;
  timezone: string;
  generated_at: string;
}

export type PlanStatus = "draft" | "active" | "paused" | "completed" | "archived";
export type PlanStepStatus = "todo" | "in_progress" | "completed" | "skipped";

export interface PersonalPlan {
  plan_id: string;
  task_id?: string;
  title: string;
  objective: string;
  status: PlanStatus;
  target_date?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface PersonalPlanStep {
  step_id: string;
  plan_id: string;
  title: string;
  detail: string;
  position: number;
  status: PlanStepStatus;
  due_at?: string;
  completed_at?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface ChecklistItem {
  item_id: string;
  task_id?: string;
  step_id?: string;
  label: string;
  checked: boolean;
  position: number;
  checked_at?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface PersonalNote {
  note_id: string;
  kind: "general" | "task" | "daily";
  title: string;
  content: string;
  task_id?: string;
  plan_id?: string;
  note_date?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface PersonaProfile {
  persona_id: string;
  user_display_name: string;
  agent_name: string;
  self_description: string;
  communication_style: string;
  preferred_tone: string;
  locale: string;
  timezone: string;
  interests: string[];
  boundaries: string[];
  onboarding_completed_at?: string;
  version: number;
}

export type EmotionState =
  | "calm"
  | "focused"
  | "curious"
  | "supportive"
  | "celebrating"
  | "reflective"
  | "resting";

export interface EmotionSnapshot {
  state: EmotionState;
  label: string;
  valence: number;
  energy: number;
  expression_hint: string;
  reason_codes: string[];
  overridden: boolean;
  updated_at: string;
}

export interface DayArchive {
  archive_id: string;
  archive_date: string;
  summary: string;
  diary: string;
  highlights: string[];
  decisions: string[];
  open_loops: string[];
  emotion_state: EmotionState;
  run_ids: string[];
  sealed_at?: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface MemoryCorrectionResult {
  status: "applied" | "needs_confirmation" | "no_match" | "invalid";
  action: "forget" | "replace" | "unknown";
  query: string;
  replacement: string;
  candidates: MemoryRecord[];
  revoked_memory_ids: string[];
  created_memory?: MemoryRecord;
  message: string;
}

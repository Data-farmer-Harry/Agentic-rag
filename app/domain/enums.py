from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    POLICY = "policy"


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    FAILED = "failed"


class IngestionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LearningJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxEventStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    DEAD_LETTER = "dead_letter"


class GraphCandidateStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    USER_ASSERTED = "user_asserted"
    OBSERVED = "observed"
    VERIFIED = "verified"


class SkillStatus(StrEnum):
    DRAFT = "draft"
    SECURITY_REVIEW = "security_review"
    OFFLINE_PASS = "offline_pass"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


class EvidenceLevel(StrEnum):
    VERIFIED = "verified"
    SUPPORTED = "supported"
    INFERRED = "inferred"
    INSUFFICIENT = "insufficient"
    CONFLICTING = "conflicting"


class AnswerMode(StrEnum):
    GROUNDED = "grounded"
    CONVERSATIONAL = "conversational"
    ACTION = "action"


class RoutingLane(StrEnum):
    DETERMINISTIC = "deterministic"
    CONVERSATION = "conversation"
    AGENT = "agent"


class WorkspaceMode(StrEnum):
    """Presentation/defaults mode for a workspace sharing the same agent kernel."""

    TEAM = "team"
    PERSONAL = "personal"


class KnowledgeLayer(StrEnum):
    """Server-owned visibility classes for retained knowledge."""

    TEAM_INTERNAL = "team_internal"
    PERSONAL = "personal"
    PUBLIC_REFERENCE = "public_reference"


class EventKind(StrEnum):
    RUN_STARTED = "run.started"
    PLAN_CREATED = "plan.created"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    EVIDENCE_ADDED = "evidence.added"
    ANSWER_COMPLETED = "answer.completed"
    FEEDBACK_RECEIVED = "feedback.received"
    RUN_COMPLETED = "run.completed"


class CapabilityEffect(StrEnum):
    READ = "read"
    WRITE = "write"


class RetryOwner(StrEnum):
    NONE = "none"
    INTEGRATION_RUNTIME = "integration_runtime"
    AGENT_RUNTIME = "agent_runtime"
    DURABLE_WORKFLOW = "durable_workflow"

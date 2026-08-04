import type {
  EntityResolutionCandidate,
  GraphCandidateCollection,
  GraphCandidateStatus,
  GraphEntityCandidate,
  GraphRelationCandidate,
  GraphResult,
  IngestionJob,
  IngestionJobSubmission,
  IngestionResult,
  KnowledgeDocument,
  DayArchive,
  EmotionSnapshot,
  EmotionState,
  ChecklistItem,
  ConversationSummary,
  ConversationMetadata,
  LearningChange,
  MemoryCorrectionResult,
  MemoryRecord,
  Overview,
  PersonaProfile,
  PersonalNote,
  PersonalPlan,
  PersonalPlanStep,
  PersonalTask,
  TaskReminderFeed,
  RunTrajectory,
  RunStartResponse,
  SampleWorkspaceImport,
  SkillDefinition,
  SkillEvolutionResult,
  SkillEvolutionSnapshot,
  PromotionDecision,
  StreamEventName
} from "./types";

const PROJECT_ID = "default";
const PERSONAL_BASE = `/v1/projects/${PROJECT_ID}/personal`;
const API_TOKEN_KEY = "hermesgraph:api-token";

export const AUTH_REQUIRED_EVENT = "hermesgraph:auth-required";

export interface AuthIdentity {
  auth_mode: "local" | "bearer";
  tenant_id: string;
  user_id: string;
  role: "viewer" | "member" | "owner";
  allowed_projects: string[];
}

export function getApiToken() {
  return window.sessionStorage.getItem(API_TOKEN_KEY)?.trim() ?? "";
}

export function setApiToken(token?: string) {
  const normalized = token?.trim();
  if (normalized) window.sessionStorage.setItem(API_TOKEN_KEY, normalized);
  else window.sessionStorage.removeItem(API_TOKEN_KEY);
}

function safeUserMessage(status: number, code?: string) {
  if (code === "forbidden" || status === 403) return "当前身份没有执行此操作的权限。";
  if (code === "not_found" || status === 404) return "当前服务暂未提供此能力，或目标已经不存在。";
  if (status === 401) return "登录状态已失效，请重新验证身份。";
  if (status === 409) return "数据刚刚发生变化，请刷新后再试。";
  if (status === 413) return "文件超过当前服务允许的大小。";
  if (status === 422) return "提交的信息不符合当前服务要求，请检查后重试。";
  if (status === 429) return "当前请求较多，请稍后重试。";
  if (status >= 500) return "服务暂时不可用，请稍后重试。";
  return "请求没有完成，请稍后重试。";
}

function errorCode(payload: unknown) {
  if (!payload || typeof payload !== "object") return undefined;
  const value = (payload as Record<string, unknown>).code;
  return typeof value === "string" ? value : undefined;
}

export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code?: string
  ) {
    super(safeUserMessage(status, code));
    this.name = "ApiRequestError";
  }
}

function authorizedOptions(options?: RequestInit): RequestInit {
  const headers = new Headers(options?.headers);
  const token = getApiToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return { ...options, headers };
}

async function authorizedFetch(path: string, options?: RequestInit) {
  const response = await fetch(path, authorizedOptions(options));
  if (response.status === 401) window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
  return response;
}

function jsonRequest(method: string, body: object): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  };
}

async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await authorizedFetch(path, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => undefined);
    throw new ApiRequestError(response.status, errorCode(payload));
  }
  return response.json() as Promise<T>;
}

async function requestBlob(path: string): Promise<Blob> {
  const response = await authorizedFetch(path);
  if (!response.ok) {
    const payload = await response.json().catch(() => undefined);
    throw new ApiRequestError(response.status, errorCode(payload));
  }
  return response.blob();
}

export const api = {
  authMe: () => requestJson<AuthIdentity>("/v1/auth/me"),
  overview: () => requestJson<Overview>(`/v1/workspace/overview?project_id=${PROJECT_ID}`),
  runs: () => requestJson<RunTrajectory[]>(`/v1/projects/${PROJECT_ID}/runs?limit=100`),
  conversations: () =>
    requestJson<ConversationSummary[]>(
      `/v1/projects/${PROJECT_ID}/conversations?limit=100&include_archived=true`
    ),
  updateConversation: (
    sessionId: string,
    patch: { title?: string; archived?: boolean }
  ) =>
    requestJson<ConversationMetadata>(
      `/v1/projects/${PROJECT_ID}/conversations/${encodeURIComponent(sessionId)}`,
      jsonRequest("PATCH", patch)
    ),
  conversationRuns: (sessionId: string) =>
    requestJson<RunTrajectory[]>(
      `/v1/projects/${PROJECT_ID}/conversations/${encodeURIComponent(sessionId)}/runs?limit=200`
    ),
  memories: (includeRevoked = false) =>
    requestJson<MemoryRecord[]>(
      `/v1/projects/${PROJECT_ID}/memories?include_revoked=${includeRevoked}`
    ),
  skills: () => requestJson<SkillDefinition[]>(`/v1/projects/${PROJECT_ID}/skills`),
  skillEvolution: () =>
    requestJson<SkillEvolutionSnapshot[]>(`/v1/projects/${PROJECT_ID}/skill-evolution`),
  evaluateSkill: (skillId: string) =>
    requestJson<SkillEvolutionResult>(
      `/v1/projects/${PROJECT_ID}/skills/${skillId}/evaluate`,
      { method: "POST" }
    ),
  changes: () =>
    requestJson<LearningChange[]>(`/v1/projects/${PROJECT_ID}/learning-changes`),
  documents: (includeArchived = false) =>
    requestJson<KnowledgeDocument[]>(
      `/v1/projects/${PROJECT_ID}/documents?include_archived=${includeArchived}`
    ),
  documentContent: (documentId: string) =>
    requestBlob(`/v1/projects/${PROJECT_ID}/documents/${encodeURIComponent(documentId)}/content`),
  uploadDocument: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return requestJson<IngestionResult>(`/v1/projects/${PROJECT_ID}/documents`, {
      method: "POST",
      body: form
    });
  },
  submitIngestionJob: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return requestJson<IngestionJobSubmission>(
      `/v1/projects/${PROJECT_ID}/ingestion-jobs`,
      { method: "POST", body: form }
    );
  },
  ingestionJobs: () =>
    requestJson<IngestionJob[]>(`/v1/projects/${PROJECT_ID}/ingestion-jobs?limit=100`),
  ingestionJob: (jobId: string) =>
    requestJson<IngestionJob>(`/v1/projects/${PROJECT_ID}/ingestion-jobs/${jobId}`),
  cancelIngestionJob: (jobId: string) =>
    requestJson<IngestionJob>(`/v1/projects/${PROJECT_ID}/ingestion-jobs/${jobId}`, {
      method: "DELETE"
    }),
  retryIngestionJob: (jobId: string) =>
    requestJson<IngestionJob>(
      `/v1/projects/${PROJECT_ID}/ingestion-jobs/${jobId}/retry`,
      { method: "POST" }
    ),
  archiveDocument: (documentId: string) =>
    requestJson<{ document_id: string; archived: boolean }>(
      `/v1/projects/${PROJECT_ID}/documents/${documentId}`,
      { method: "DELETE" }
    ),
  revokeMemory: (memoryId: string) =>
    requestJson<{ revoked: boolean }>(`/v1/projects/${PROJECT_ID}/memories/${memoryId}`, {
      method: "DELETE"
    }),
  remember: (summary: string, sourceSessionId?: string) =>
    requestJson<MemoryRecord>(
      `/v1/projects/${PROJECT_ID}/memories`,
      jsonRequest("POST", {
        summary,
        memory_type: "semantic",
        source_session_id: sourceSessionId
      })
    ),
  personalTasks: () => requestJson<PersonalTask[]>(`${PERSONAL_BASE}/tasks`),
  createPersonalTask: (task: {
    title: string;
    description?: string;
    priority?: number;
    due_at?: string;
    tags?: string[];
  }) => requestJson<PersonalTask>(`${PERSONAL_BASE}/tasks`, jsonRequest("POST", task)),
  updatePersonalTask: (taskId: string, patch: object) =>
    requestJson<PersonalTask>(
      `${PERSONAL_BASE}/tasks/${taskId}`,
      jsonRequest("PATCH", patch)
    ),
  taskReminders: () => requestJson<TaskReminderFeed>(`${PERSONAL_BASE}/reminders`),
  markTaskReminderRead: (taskId: string) =>
    requestJson<TaskReminderFeed>(
      `${PERSONAL_BASE}/reminders/${taskId}/read`,
      { method: "PUT" }
    ),
  markAllTaskRemindersRead: () =>
    requestJson<TaskReminderFeed>(`${PERSONAL_BASE}/reminders/read-all`, {
      method: "PUT"
    }),
  snoozeTaskReminder: (taskId: string, durationMinutes = 60) =>
    requestJson<TaskReminderFeed>(
      `${PERSONAL_BASE}/reminders/${taskId}/snooze`,
      jsonRequest("PUT", { duration_minutes: durationMinutes })
    ),
  personalPlans: () => requestJson<PersonalPlan[]>(`${PERSONAL_BASE}/plans`),
  createPersonalPlan: (plan: {
    task_id?: string;
    title: string;
    objective?: string;
    target_date?: string;
  }) => requestJson<PersonalPlan>(`${PERSONAL_BASE}/plans`, jsonRequest("POST", plan)),
  updatePersonalPlan: (planId: string, patch: object) =>
    requestJson<PersonalPlan>(
      `${PERSONAL_BASE}/plans/${planId}`,
      jsonRequest("PATCH", patch)
    ),
  planSteps: (planId: string) =>
    requestJson<PersonalPlanStep[]>(`${PERSONAL_BASE}/plans/${planId}/steps`),
  createPlanStep: (planId: string, step: { title: string; detail?: string }) =>
    requestJson<PersonalPlanStep>(
      `${PERSONAL_BASE}/plans/${planId}/steps`,
      jsonRequest("POST", step)
    ),
  updatePlanStep: (stepId: string, patch: object) =>
    requestJson<PersonalPlanStep>(
      `${PERSONAL_BASE}/plan-steps/${stepId}`,
      jsonRequest("PATCH", patch)
    ),
  checklist: (taskId?: string, stepId?: string) => {
    const query = new URLSearchParams();
    if (taskId) query.set("task_id", taskId);
    if (stepId) query.set("step_id", stepId);
    return requestJson<ChecklistItem[]>(
      `${PERSONAL_BASE}/checklist${query.size ? `?${query}` : ""}`
    );
  },
  createChecklistItem: (item: {
    task_id?: string;
    step_id?: string;
    label: string;
  }) =>
    requestJson<ChecklistItem>(
      `${PERSONAL_BASE}/checklist`,
      jsonRequest("POST", item)
    ),
  updateChecklistItem: (itemId: string, patch: object) =>
    requestJson<ChecklistItem>(
      `${PERSONAL_BASE}/checklist/${itemId}`,
      jsonRequest("PATCH", patch)
    ),
  personalNotes: (taskId?: string, noteDate?: string) => {
    const query = new URLSearchParams();
    if (taskId) query.set("task_id", taskId);
    if (noteDate) query.set("note_date", noteDate);
    return requestJson<PersonalNote[]>(
      `${PERSONAL_BASE}/notes${query.size ? `?${query}` : ""}`
    );
  },
  upsertPersonalNote: (note: object) =>
    requestJson<PersonalNote>(`${PERSONAL_BASE}/notes`, jsonRequest("POST", note)),
  persona: () => requestJson<PersonaProfile>(`${PERSONAL_BASE}/persona`),
  updatePersona: (patch: object) =>
    requestJson<PersonaProfile>(`${PERSONAL_BASE}/persona`, jsonRequest("PUT", patch)),
  dayArchives: (dateFrom: string, dateTo: string) =>
    requestJson<DayArchive[]>(
      `${PERSONAL_BASE}/days?date_from=${dateFrom}&date_to=${dateTo}`
    ),
  sealDay: (archiveDate: string, force = false) =>
    requestJson<DayArchive>(
      `${PERSONAL_BASE}/days/${archiveDate}/seal?force=${force}`,
      { method: "POST" }
    ),
  updateDay: (archiveDate: string, patch: object) =>
    requestJson<DayArchive>(
      `${PERSONAL_BASE}/days/${archiveDate}`,
      jsonRequest("PUT", patch)
    ),
  emotion: () => requestJson<EmotionSnapshot>(`${PERSONAL_BASE}/emotion`),
  setEmotion: (state: EmotionState, note: string, durationMinutes: number) =>
    requestJson<EmotionSnapshot>(
      `${PERSONAL_BASE}/emotion/override`,
      jsonRequest("PUT", {
        state,
        note,
        duration_minutes: durationMinutes
      })
    ),
  clearEmotion: () =>
    requestJson<EmotionSnapshot>(`${PERSONAL_BASE}/emotion/override`, {
      method: "DELETE"
    }),
  correctMemory: (request: string, confirmMemoryIds: string[] = []) =>
    requestJson<MemoryCorrectionResult>(
      `${PERSONAL_BASE}/memory-corrections`,
      jsonRequest("POST", {
        request,
        confirm_memory_ids: confirmMemoryIds
      })
    ),
  transitionSkill: (skillId: string, targetStatus: string, humanApproved = false) =>
    requestJson<PromotionDecision>(
      `/v1/projects/${PROJECT_ID}/skills/${skillId}/transition`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_status: targetStatus, human_approved: humanApproved })
      }
    ),
  graphSearch: (entities: string[], template = "paths") =>
    requestJson<GraphResult>(`/v1/projects/${PROJECT_ID}/graph/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entities, template, max_hops: 3, limit: 20 })
    }),
  /**
   * Fixed system-map actions deliberately remain mapped to the existing
   * allowlisted graph templates. No arbitrary Cypher or free-form graph query
   * is ever sent from the browser.
   */
  systemMapQuery: (
    kind: "dependencies" | "impact" | "ownership" | "incidents" | "decisions" | "compare",
    entities: string[]
  ) => {
    const template = kind === "dependencies" || kind === "ownership"
      ? "neighbors"
      : kind === "decisions"
        ? "conflicts"
        : "paths";
    return requestJson<GraphResult>(`/v1/projects/${PROJECT_ID}/graph/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entities, template, max_hops: 3, limit: 20 })
    });
  },
  startEnterpriseFixtureImport: () =>
    requestJson<SampleWorkspaceImport>(
      `/v1/projects/${PROJECT_ID}/enterprise-fixture/runs`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dry_run: false }) }
    ),
  enterpriseFixtureImportStatus: (runId: string) =>
    requestJson<SampleWorkspaceImport>(
      `/v1/projects/${PROJECT_ID}/enterprise-fixture/runs/${encodeURIComponent(runId)}`
    ),
  graphCandidates: (status?: GraphCandidateStatus) =>
    requestJson<GraphCandidateCollection>(
      `/v1/projects/${PROJECT_ID}/graph/candidates${status ? `?status=${status}` : ""}`
    ),
  reviewGraphEntity: (candidateId: string, targetStatus: GraphCandidateStatus) =>
    requestJson<GraphEntityCandidate>(
      `/v1/projects/${PROJECT_ID}/graph/candidates/entities/${candidateId}/review`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_status: targetStatus,
          reason: "Reviewed from the HermesGraph workbench."
        })
      }
    ),
  reviewGraphRelation: (candidateId: string, targetStatus: GraphCandidateStatus) =>
    requestJson<GraphRelationCandidate>(
      `/v1/projects/${PROJECT_ID}/graph/candidates/relations/${candidateId}/review`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_status: targetStatus,
          reason: "Reviewed from the HermesGraph workbench."
        })
      }
    ),
  reviewEntityResolution: (candidateId: string, targetStatus: GraphCandidateStatus) =>
    requestJson<EntityResolutionCandidate>(
      `/v1/projects/${PROJECT_ID}/graph/candidates/resolutions/${candidateId}/review`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_status: targetStatus,
          reason: "Reviewed from the HermesGraph workbench."
        })
      }
    ),
  feedback: (runId: string, score: number, text?: string) =>
    requestJson(`/v1/runs/${runId}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ score, text })
    }),
  startRun: (
    input: string,
    domainPack: string,
    sessionId: string,
    idempotencyKey: string,
    signal?: AbortSignal
  ) =>
    requestJson<RunStartResponse>(`/v1/projects/${PROJECT_ID}/runs/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        input,
        domain_pack: domainPack,
        session_id: sessionId,
        idempotency_key: idempotencyKey
      }),
      signal
    }),
  cancelRun: (runId: string) =>
    requestJson<RunTrajectory>(
      `/v1/projects/${PROJECT_ID}/runs/${encodeURIComponent(runId)}`,
      { method: "DELETE" }
    )
};

export async function consumeRunEvents(
  runId: string,
  afterCursor: number,
  onEvent: (event: StreamEventName, payload: any, cursor: number) => void,
  onReconnect?: (attempt: number) => void,
  signal?: AbortSignal
): Promise<number> {
  let cursor = afterCursor;
  let reconnectAttempt = 0;
  let terminal = false;
  while (!terminal) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    try {
      const query = new URLSearchParams({ after_cursor: String(cursor) });
      const response = await authorizedFetch(
        `/v1/projects/${PROJECT_ID}/runs/${encodeURIComponent(runId)}/events/stream?${query}`,
        { headers: { Accept: "text/event-stream" }, signal }
      );
      if (!response.ok || !response.body) {
        throw new RunStreamError({
          code: response.status === 403 ? "forbidden" : "run_stream_unavailable",
          retryable: response.status >= 500
        });
      }
      reconnectAttempt = 0;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() ?? "";
        for (const block of blocks) {
          let event = "message";
          let eventCursor = cursor;
          const data: string[] = [];
          for (const line of block.split("\n")) {
            if (line.startsWith("id:")) eventCursor = Number(line.slice(3).trim()) || cursor;
            if (line.startsWith("event:")) event = line.slice(6).trim();
            if (line.startsWith("data:")) data.push(line.slice(5).trim());
          }
          if (event !== "message" && data.length && eventCursor > cursor) {
            cursor = eventCursor;
            terminal = ["run.completed", "run.cancelled", "run.error"].includes(event);
            try {
              onEvent(event as StreamEventName, JSON.parse(data.join("\n")), cursor);
            } catch (cause) {
              if (cause instanceof RunStreamError) throw cause;
              throw new RunStreamError({ code: "stream_protocol_error", retryable: true });
            }
          }
        }
        if (done || terminal) break;
      }
      if (terminal) return cursor;
    } catch (error) {
      if (signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw error;
      if (terminal) throw error;
      if (error instanceof SyntaxError) {
        throw new RunStreamError({ code: "stream_protocol_error", retryable: true });
      }
      if (error instanceof RunStreamError && !error.retryable) throw error;
      reconnectAttempt += 1;
      if (reconnectAttempt > 4) {
        throw new RunStreamError({ code: "stream_connection_failed", retryable: true });
      }
      onReconnect?.(reconnectAttempt);
      await new Promise((resolve) => window.setTimeout(resolve, Math.min(3_000, 400 * reconnectAttempt)));
    }
  }
  return cursor;
}

function safeRunMessage(code?: string) {
  if (code === "forbidden") return "当前身份没有运行或查看这项任务的权限。";
  if (code === "run_stream_unavailable" || code === "stream_connection_failed") {
    return "运行连接暂时不可用，已保留任务状态，可以稍后重试。";
  }
  if (code === "stream_protocol_error") return "运行事件格式异常，请稍后重试。";
  if (code === "model_unavailable" || code === "provider_unavailable") {
    return "模型服务暂时不可用，请稍后重试。";
  }
  return "这次任务没有完成，请重新发送后再试。";
}

export class RunStreamError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly phase?: string;
  readonly durationMs?: number;

  constructor(payload: {
    code?: string;
    message?: string;
    retryable?: boolean;
    phase?: string;
    duration_ms?: number;
  }) {
    super(safeRunMessage(payload.code));
    this.name = "RunStreamError";
    this.code = payload.code || "run_failed";
    this.retryable = payload.retryable ?? true;
    this.phase = payload.phase;
    this.durationMs = payload.duration_ms;
  }
}

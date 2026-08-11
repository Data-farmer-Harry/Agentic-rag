import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, LogOut, PanelRight, RefreshCw, Search, X } from "lucide-react";
import {
  api,
  AUTH_REQUIRED_EVENT,
  consumeRunEvents,
  ApiRequestError,
  RunStreamError,
  setApiToken,
  type AuthIdentity
} from "./api";
import { ChatView } from "./components/ChatView";
import { CommandPalette } from "./components/CommandPalette";
import { Inspector } from "./components/Inspector";
import { NotificationCenter } from "./components/NotificationCenter";
import { Sidebar, type ViewName } from "./components/Sidebar";
import type {
  ConversationMessage,
  ConversationSummary,
  Evidence,
  KnowledgeDocument,
  LearningChange,
  MemoryRecord,
  Overview,
  PersonaProfile,
  RunTrajectory,
  SkillEvolutionSnapshot,
  StreamEventName,
  TaskReminderFeed,
  ToolEvent,
  WorkspaceProfile,
  RunCompletedEvent,
  RetrievalRouteDecision
} from "./types";

const ActionsView = lazy(() => import("./components/ActionsView").then((module) => ({ default: module.ActionsView })));
const ReviewView = lazy(() => import("./components/ReviewView").then((module) => ({ default: module.ReviewView })));
const ProfileView = lazy(() => import("./components/ProfileView").then((module) => ({ default: module.ProfileView })));
const AuthDialog = lazy(() => import("./components/AuthDialog").then((module) => ({ default: module.AuthDialog })));
const OnboardingDialog = lazy(() => import("./components/OnboardingDialog").then((module) => ({ default: module.OnboardingDialog })));
const GraphView = lazy(() => import("./components/DataViews").then((module) => ({ default: module.GraphView })));
const KnowledgeView = lazy(() => import("./components/DataViews").then((module) => ({ default: module.KnowledgeView })));
const LearningView = lazy(() => import("./components/DataViews").then((module) => ({ default: module.LearningView })));
const MemoryView = lazy(() => import("./components/DataViews").then((module) => ({ default: module.MemoryView })));
const RunsView = lazy(() => import("./components/DataViews").then((module) => ({ default: module.RunsView })));
const SkillsView = lazy(() => import("./components/DataViews").then((module) => ({ default: module.SkillsView })));

function ViewLoadingFallback() {
  return (
    <section className="view-loading" role="status">
      <RefreshCw className="spin" size={18} />
      <span>正在载入工作区视图</span>
    </section>
  );
}

const ACTIVE_SESSION_KEY = "hermesgraph:active-session";
const ONBOARDING_DISMISSED_KEY = "hermesgraph:onboarding-dismissed-at";
const REMINDER_NOTIFICATION_KEYS = "hermesgraph:desktop-reminder-keys";
const ATTACHMENT_BLOCK = /\n\n<attachments>\n([\s\S]*?)\n<\/attachments>\s*$/;

interface ActiveRunState {
  runId: string;
  assistantId: string;
  cursor: number;
  sessionId: string;
}

function createSessionId() {
  return `chat-${crypto.randomUUID()}`;
}

function initialSessionId() {
  return window.localStorage.getItem(ACTIVE_SESSION_KEY) || createSessionId();
}

function parseUserInput(input: string) {
  const match = input.match(ATTACHMENT_BLOCK);
  if (!match) return { content: input, attachments: [] as string[] };
  return {
    content: input.slice(0, match.index).trim(),
    attachments: match[1]
      .split("\n")
      .map((line) => line.replace(/^-\s*/, "").trim())
      .filter(Boolean)
  };
}

function inputWithAttachments(input: string, attachments: string[]) {
  if (attachments.length === 0) return input;
  const safeNames = attachments.map((name) =>
    name.replace(/[<>\r\n]/g, " ").replace(/\s+/g, " ").trim()
  );
  return `${input}\n\n<attachments>\n${safeNames.map((name) => `- ${name}`).join("\n")}\n</attachments>`;
}

function desktopNotificationPermission(): NotificationPermission | "unsupported" {
  return "Notification" in window ? window.Notification.permission : "unsupported";
}

function emitDesktopReminders(feed: TaskReminderFeed) {
  if (!("Notification" in window) || window.Notification.permission !== "granted") return;
  let seen = new Set<string>();
  try {
    seen = new Set(JSON.parse(window.localStorage.getItem(REMINDER_NOTIFICATION_KEYS) ?? "[]"));
  } catch {
    seen = new Set();
  }
  const eligible = feed.items.filter(
    (item) =>
      item.unread
      && item.kind !== "today"
      && !seen.has(`${item.task_id}:${item.due_at}:${item.kind}`)
  );
  if (eligible.length === 0) return;
  const first = eligible[0];
  const suffix = eligible.length > 1 ? `，另有 ${eligible.length - 1} 项` : "";
  try {
    new window.Notification(
      first.kind === "overdue" ? "任务已逾期" : "任务即将到期",
      {
        body: `${first.title}${suffix}`,
        tag: "hermesgraph-task-reminders"
      }
    );
  } catch (error) {
    console.error("Unable to show desktop reminder", error);
    return;
  }
  eligible.forEach((item) => seen.add(`${item.task_id}:${item.due_at}:${item.kind}`));
  window.localStorage.setItem(
    REMINDER_NOTIFICATION_KEYS,
    JSON.stringify(Array.from(seen).slice(-200))
  );
}

function messagesFromRuns(runs: RunTrajectory[]): ConversationMessage[] {
  return runs.flatMap((run) => {
    const startedAt = new Date(run.context.started_at);
    const completedAt = run.completed_at ? new Date(run.completed_at) : undefined;
    const parsedInput = parseUserInput(run.user_input);
    const isRunning = run.status === "running";
    const user: ConversationMessage = {
      id: `${run.context.run_id}:user`,
      role: "user",
      content: parsedInput.content,
      createdAt: startedAt,
      attachments: parsedInput.attachments
    };
    const assistant: ConversationMessage = {
      id: `${run.context.run_id}:assistant`,
      role: "assistant",
      content: run.answer?.answer_markdown ?? "",
      createdAt: completedAt ?? startedAt,
      runId: run.context.run_id,
      status: run.answer
        ? "完成"
        : isRunning
          ? "任务仍在运行"
        : run.status === "failed"
          ? "失败"
          : run.status === "cancelled"
            ? "已停止"
            : "未完成",
      phase: run.answer ? "completed" : "executing",
      confidence: run.answer?.confidence,
      responseMode: run.answer?.response_mode,
      citations: run.answer?.citations ?? [],
      memoryIds: run.answer?.memory_ids ?? [],
      graphPaths: run.answer?.graph_paths ?? [],
      followUpActions: run.answer?.follow_up_actions ?? [],
      limitations: run.answer?.limitations ?? [],
      toolEvents: run.tool_events,
      durationMs: completedAt
        ? Math.max(0, completedAt.getTime() - startedAt.getTime())
        : isRunning
          ? Math.max(0, Date.now() - startedAt.getTime())
          : undefined,
      feedbackScore: run.feedback_score,
      retryable: run.status === "failed" || run.status === "cancelled",
      cancelled: run.status === "cancelled",
      streaming: isRunning,
      error: run.answer
        ? undefined
        : isRunning
          ? undefined
        : run.status === "failed"
          ? "这次任务未能完成，可以重新发送后再试。"
          : run.status === "cancelled"
            ? "这次任务已停止，可以继续编辑后重新发送。"
            : "这次任务没有完成，可以重新发送后再试。"
    };
    return [user, assistant];
  });
}

const viewTitles: Record<ViewName, string> = {
  chat: "对话",
  runs: "运行",
  knowledge: "知识",
  graph: "系统地图",
  memory: "我的记忆",
  actions: "工作",
  review: "日历回顾",
  profile: "个人设置",
  skills: "技能治理",
  learning: "学习"
};

const viewSections: Record<ViewName, string> = {
  chat: "工作台",
  actions: "工作台",
  review: "工作",
  knowledge: "工作台",
  graph: "工作台",
  memory: "学习",
  runs: "工作台",
  skills: "运行",
  learning: "工作台",
  profile: "设置"
};

function learningModeLabel(mode?: string) {
  const labels: Record<string, string> = {
    disabled: "学习已关闭",
    shadow: "影子学习",
    review: "等待确认",
    active: "持续学习"
  };
  return labels[mode ?? ""] ?? "影子学习";
}

function App() {
  const [identity, setIdentity] = useState<AuthIdentity>();
  const [authChecking, setAuthChecking] = useState(true);
  const [authRequired, setAuthRequired] = useState(false);
  const [authError, setAuthError] = useState<string>();
  const [view, setView] = useState<ViewName>("chat");
  const [overview, setOverview] = useState<Overview>();
  const [workspaceProfile, setWorkspaceProfile] = useState<WorkspaceProfile>();
  const [workspaceError, setWorkspaceError] = useState<string>();
  const [sessionError, setSessionError] = useState<string>();
  const [runs, setRuns] = useState<RunTrajectory[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [skillEvolution, setSkillEvolution] = useState<SkillEvolutionSnapshot[]>([]);
  const [changes, setChanges] = useState<LearningChange[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [sessionId, setSessionId] = useState(initialSessionId);
  const [conversationLoading, setConversationLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [statusLabel, setStatusLabel] = useState("就绪");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [domainPack, setDomainPack] = useState("general");
  const [traceTools, setTraceTools] = useState<ToolEvent[]>([]);
  const [traceEvidence, setTraceEvidence] = useState<Evidence[]>([]);
  const [selectedEvidence, setSelectedEvidence] = useState<Evidence>();
  const [selectedRun, setSelectedRun] = useState<RunTrajectory>();
  const [refreshing, setRefreshing] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);
  const [persona, setPersona] = useState<PersonaProfile>();
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [focusedTaskId, setFocusedTaskId] = useState<string>();
  const [focusedReviewDate, setFocusedReviewDate] = useState<string>();
  const [resumeCandidate, setResumeCandidate] = useState<RunTrajectory>();
  const [reminderFeed, setReminderFeed] = useState<TaskReminderFeed>();
  const [reminderRefreshing, setReminderRefreshing] = useState(false);
  const [desktopPermission, setDesktopPermission] = useState<
    NotificationPermission | "unsupported"
  >(desktopNotificationPermission);
  const abortRef = useRef<AbortController | undefined>(undefined);
  const activeRunRef = useRef<ActiveRunState | undefined>(undefined);
  const recoveringRunsRef = useRef(new Set<string>());
  const conversationRequestRef = useRef(0);
  const loadedConversationSessionRef = useRef<string | undefined>(undefined);
  const failedConversationRef = useRef<{
    sessionId: string;
    commitSession: boolean;
  } | undefined>(undefined);
  const workspaceRefreshRequestRef = useRef(0);
  const stopRequestedRef = useRef(false);
  const domainPackInitializedRef = useRef(false);

  function invalidateAsyncWorkspaceLoads() {
    conversationRequestRef.current += 1;
    workspaceRefreshRequestRef.current += 1;
    loadedConversationSessionRef.current = undefined;
  }

  const sampleImportAvailable = Boolean(
    overview?.capabilities.includes("enterprise_fixture_import")
  );

  useEffect(() => {
    let active = true;
    function requireAuthentication() {
      if (!active) return;
      invalidateAsyncWorkspaceLoads();
      setIdentity(undefined);
      setAuthRequired(true);
      setAuthChecking(false);
    }
    window.addEventListener(AUTH_REQUIRED_EVENT, requireAuthentication);
    void api.authMe().then((currentIdentity) => {
      if (!active) return;
      setIdentity(currentIdentity);
      setAuthRequired(false);
      setAuthError(undefined);
    }).catch(() => {
      if (!active) return;
      setApiToken();
      setAuthRequired(true);
      setAuthError("访问令牌无效或已失效，请重新输入。");
    }).finally(() => {
      if (active) setAuthChecking(false);
    });
    return () => {
      active = false;
      window.removeEventListener(AUTH_REQUIRED_EVENT, requireAuthentication);
    };
  }, []);

  const refreshReminders = useCallback(async () => {
    if (!identity) return;
    setReminderRefreshing(true);
    try {
      const feed = await api.taskReminders();
      setReminderFeed(feed);
      setDesktopPermission(desktopNotificationPermission());
      emitDesktopReminders(feed);
    } catch (error) {
      console.error("Unable to refresh task reminders", error);
    } finally {
      setReminderRefreshing(false);
    }
  }, [identity]);

  const refresh = useCallback(async () => {
    if (!identity) return;
    const requestId = ++workspaceRefreshRequestRef.current;
    setRefreshing(true);
    try {
      const overviewData = await api.overview();
      if (requestId !== workspaceRefreshRequestRef.current) return;

      setOverview(overviewData);
      const profile = overviewData.workspace_profile ?? undefined;
      setWorkspaceProfile(profile);
      setWorkspaceError(undefined);
      if (!domainPackInitializedRef.current) {
        const preferredDomainPack = (
          profile?.default_domain_pack && overviewData.domain_packs.includes(profile.default_domain_pack)
            ? profile.default_domain_pack
            : overviewData.domain_packs.find((pack) =>
                ["software_engineering", "software_docs", "software_docs_reference"].includes(pack)
              ) ?? overviewData.domain_packs[0]
        );
        if (preferredDomainPack) setDomainPack(preferredDomainPack);
        domainPackInitializedRef.current = true;
      }

      const [
        runResult,
        documentResult,
        memoryResult,
        evolutionResult,
        changeResult,
        conversationResult
      ] = await Promise.allSettled([
        api.runs(),
        api.documents(true),
        api.memories(true),
        api.skillEvolution(),
        api.changes(),
        api.conversations()
      ]);
      if (requestId !== workspaceRefreshRequestRef.current) return;

      const unavailableAreas: string[] = [];
      if (runResult.status === "fulfilled") setRuns(runResult.value);
      else unavailableAreas.push("运行记录");
      if (documentResult.status === "fulfilled") setDocuments(documentResult.value);
      else unavailableAreas.push("知识资料");
      if (memoryResult.status === "fulfilled") setMemories(memoryResult.value);
      else unavailableAreas.push("记忆");
      if (evolutionResult.status === "fulfilled") setSkillEvolution(evolutionResult.value);
      else unavailableAreas.push("技能治理");
      if (changeResult.status === "fulfilled") setChanges(changeResult.value);
      else unavailableAreas.push("学习变更");
      if (conversationResult.status === "fulfilled") setConversations(conversationResult.value);
      else unavailableAreas.push("对话列表");

      if (unavailableAreas.length > 0) {
        setWorkspaceError(
          `部分工作区信息未能刷新（${unavailableAreas.join("、")}），正在保留上次可用数据。`
        );
      }
    } catch (error) {
      if (requestId !== workspaceRefreshRequestRef.current) return;
      setWorkspaceError(
        error instanceof ApiRequestError
          ? error.message
          : "无法刷新工作区状态，请检查网络后重试。"
      );
    } finally {
      if (requestId === workspaceRefreshRequestRef.current) setRefreshing(false);
    }
  }, [identity]);

  useEffect(() => {
    if (identity) void refresh();
  }, [identity, refresh]);

  useEffect(() => {
    if (!identity) return;
    void refreshReminders();
    const timer = window.setInterval(() => void refreshReminders(), 60_000);
    function refreshWhenVisible() {
      if (document.visibilityState === "visible") void refreshReminders();
    }
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [identity, refreshReminders]);

  useEffect(() => {
    if (!identity) return;
    void api.persona().then((nextPersona) => {
      setPersona(nextPersona);
      const dismissedAt = Number(window.localStorage.getItem(ONBOARDING_DISMISSED_KEY));
      const dismissalExpired = !dismissedAt || Date.now() - dismissedAt > 24 * 60 * 60 * 1_000;
      setOnboardingOpen(!nextPersona.onboarding_completed_at && dismissalExpired);
    }).catch((error) => console.error("Unable to load persona", error));
  }, [identity]);

  const loadConversation = useCallback(async (
    nextSessionId: string,
    options: { commitSession?: boolean } = {}
  ) => {
    if (!identity) return;
    const requestId = ++conversationRequestRef.current;
    const commitSession = options.commitSession === true;
    setConversationLoading(true);
    setSessionError(undefined);
    try {
      const sessionRuns = await api.conversationRuns(nextSessionId);
      if (requestId !== conversationRequestRef.current) return;
      setMessages(messagesFromRuns(sessionRuns));
      setTraceTools([]);
      setTraceEvidence([]);
      setSelectedEvidence(undefined);
      setSelectedRun(undefined);
      const latest = sessionRuns.at(-1);
      if (latest) setDomainPack(latest.context.domain_pack);
      setResumeCandidate(latest?.status === "running" ? latest : undefined);
      loadedConversationSessionRef.current = nextSessionId;
      failedConversationRef.current = undefined;
      if (commitSession) setSessionId(nextSessionId);
    } catch (error) {
      if (requestId !== conversationRequestRef.current) return;
      failedConversationRef.current = { sessionId: nextSessionId, commitSession };
      setSessionError(
        error instanceof ApiRequestError
          ? `${error.message}，已保留当前会话内容。`
          : "无法恢复所选对话，已保留当前会话内容。"
      );
    } finally {
      if (requestId === conversationRequestRef.current) setConversationLoading(false);
    }
  }, [identity]);

  useEffect(() => {
    window.localStorage.setItem(ACTIVE_SESSION_KEY, sessionId);
    if (identity && loadedConversationSessionRef.current !== sessionId) {
      void loadConversation(sessionId);
    }
  }, [identity, loadConversation, sessionId]);

  useEffect(() => {
    if (!resumeCandidate || recoveringRunsRef.current.has(resumeCandidate.context.run_id)) return;
    void resumeExistingRun(resumeCandidate);
  }, [resumeCandidate]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((current) => !current);
      }
      if (event.key === "Escape") setCommandOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function updateAssistant(id: string, update: (message: ConversationMessage) => ConversationMessage) {
    setMessages((current) => current.map((message) => (message.id === id ? update(message) : message)));
  }

  function applyRunEvent(
    runId: string,
    assistantId: string,
    event: StreamEventName,
    payload: any,
    cursor: number
  ) {
    const active = activeRunRef.current;
    if (active?.runId === runId) activeRunRef.current = { ...active, cursor };
    if (event === "run.accepted") {
      updateAssistant(assistantId, (message) => ({
        ...message,
        runId: payload.run_id,
        phase: "accepted"
      }));
    }
    if (event === "run.route") {
      updateAssistant(assistantId, (message) => ({
        ...message,
        retrievalRoute: payload as RetrievalRouteDecision
      }));
    }
    if (event === "run.status") {
      setStatusLabel(payload.label);
      updateAssistant(assistantId, (message) => ({
        ...message,
        status: payload.label,
        phase: payload.phase
      }));
    }
    if (event === "run.heartbeat") {
      setElapsedMs(Number(payload.elapsed_ms) || 0);
      if (typeof payload.label === "string" && payload.label) {
        setStatusLabel(payload.label);
        updateAssistant(assistantId, (message) => ({
          ...message,
          status: payload.label,
          phase: payload.phase === "understanding" ? "understanding" : "executing"
        }));
      }
    }
    if (event === "tool.completed") {
      const tool = payload as ToolEvent;
      const key = `${tool.tool_name}:${tool.input_hash}:${tool.created_at}`;
      setTraceTools((current) =>
        current.some((item) => `${item.tool_name}:${item.input_hash}:${item.created_at}` === key)
          ? current
          : [...current, tool]
      );
      updateAssistant(assistantId, (message) => ({
        ...message,
        toolEvents: (message.toolEvents ?? []).some(
          (item) => `${item.tool_name}:${item.input_hash}:${item.created_at}` === key
        )
          ? message.toolEvents
          : [...(message.toolEvents ?? []), tool]
      }));
    }
    if (event === "answer.delta") {
      updateAssistant(assistantId, (message) => ({
        ...message,
        content: message.content + payload.delta,
        status: "正在显示回答",
        phase: "synthesizing"
      }));
      setStatusLabel("正在显示回答");
    }
    if (event === "evidence.added") {
      const evidence = payload as Evidence;
      setTraceEvidence((current) =>
        current.some((item) => item.evidence_id === evidence.evidence_id)
          ? current
          : [...current, evidence]
      );
      updateAssistant(assistantId, (message) => ({
        ...message,
        citations: (message.citations ?? []).some(
          (item) => item.evidence_id === evidence.evidence_id
        )
          ? message.citations
          : [...(message.citations ?? []), evidence]
      }));
    }
    if (event === "learning.updated") {
      setStatusLabel(`已记录 ${payload.count} 项学习变更`);
    }
    if (event === "run.completed") {
      const completed = payload as RunCompletedEvent;
      updateAssistant(assistantId, (message) => ({
        ...message,
        content: completed.answer.answer_markdown,
        runId: completed.run_id,
        confidence: completed.answer.confidence,
        responseMode: completed.answer.response_mode,
        citations: completed.answer.citations,
        memoryIds: completed.answer.memory_ids ?? [],
        graphPaths: completed.answer.graph_paths,
        followUpActions: completed.answer.follow_up_actions,
        limitations: completed.answer.limitations,
        toolEvents: completed.tool_events,
        retrievalRoute: completed.retrieval_route ?? message.retrievalRoute,
        durationMs: completed.duration_ms,
        learningCount: completed.learning_change_count,
        status: "完成",
        phase: "completed",
        retryable: false,
        cancelled: false,
        streaming: false,
        error: undefined
      }));
      setTraceTools(completed.tool_events);
      setTraceEvidence(completed.answer.citations);
      setStatusLabel("完成");
      setElapsedMs(Number(completed.duration_ms) || 0);
    }
    if (event === "run.cancelled") {
      updateAssistant(assistantId, (message) => ({
        ...message,
        streaming: false,
        status: "已停止",
        phase: "executing",
        durationMs: payload.duration_ms,
        cancelled: true,
        retryable: true,
        error: "任务已停止。你可以修改请求后重新开始。"
      }));
      setStatusLabel("已停止");
      setElapsedMs(Number(payload.duration_ms) || 0);
    }
    if (event === "run.error") throw new RunStreamError(payload);
  }

  async function followRun(
    runId: string,
    assistantId: string,
    controller: AbortController,
    afterCursor = 0
  ) {
    return consumeRunEvents(
      runId,
      afterCursor,
      (event, payload, cursor) => applyRunEvent(runId, assistantId, event, payload, cursor),
      (attempt) => {
        const label = `连接中断，正在第 ${attempt} 次恢复`;
        setStatusLabel(label);
        updateAssistant(assistantId, (message) => ({
          ...message,
          status: label,
          streaming: true
        }));
      },
      controller.signal
    );
  }

  function markRunFailure(
    assistantId: string,
    error: unknown,
    startedAt: number,
    aborted: boolean
  ) {
    const streamError = error instanceof RunStreamError ? error : undefined;
    const durationMs = streamError?.durationMs ?? Math.round(performance.now() - startedAt);
    updateAssistant(assistantId, (message) => ({
      ...message,
      streaming: false,
      status: aborted ? "已停止" : "失败",
      phase: "executing",
      durationMs,
      cancelled: aborted,
      retryable: aborted || (streamError?.retryable ?? true),
      errorCode: streamError?.code,
      error: aborted
        ? "任务已停止。你可以修改请求后重新开始。"
        : streamError?.message ??
          (error instanceof ApiRequestError
            ? error.message
            : "任务连接没有完成，请检查网络后重试。")
    }));
    setElapsedMs(durationMs);
    setStatusLabel(aborted ? "已停止" : "运行失败");
  }

  async function submitTask(input: string, attachments: string[] = []) {
    if (running) return;
    const visibleInput = input.trim() || "请阅读并总结附件。";
    const agentInput = inputWithAttachments(visibleInput, attachments);
    const now = new Date();
    const assistantId = crypto.randomUUID();
    setMessages((current) => [
      ...current,
      {
        id: crypto.randomUUID(),
        role: "user",
        content: visibleInput,
        createdAt: now,
        attachments
      },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        createdAt: now,
        status: "正在理解问题",
        phase: "accepted",
        streaming: true,
        citations: [],
        toolEvents: []
      }
    ]);
    setRunning(true);
    setElapsedMs(0);
    setStatusLabel("正在理解问题");
    setTraceTools([]);
    setTraceEvidence([]);
    setSelectedEvidence(undefined);
    setSelectedRun(undefined);
    stopRequestedRef.current = false;
    const controller = new AbortController();
    abortRef.current = controller;
    const startedAt = performance.now();

    try {
      const started = await api.startRun(
        agentInput,
        domainPack,
        sessionId,
        crypto.randomUUID()
      );
      activeRunRef.current = {
        runId: started.run_id,
        assistantId,
        cursor: 0,
        sessionId
      };
      updateAssistant(assistantId, (message) => ({
        ...message,
        runId: started.run_id
      }));
      if (stopRequestedRef.current) {
        await api.cancelRun(started.run_id);
        controller.abort();
        throw new DOMException("Aborted", "AbortError");
      }
      await followRun(started.run_id, assistantId, controller);
    } catch (error) {
      const aborted = error instanceof Error && error.name === "AbortError";
      markRunFailure(assistantId, error, startedAt, aborted);
    } finally {
      setRunning(false);
      if (activeRunRef.current?.assistantId === assistantId) activeRunRef.current = undefined;
      abortRef.current = undefined;
      stopRequestedRef.current = false;
      await refresh();
    }
  }

  async function resumeExistingRun(run: RunTrajectory) {
    const runId = run.context.run_id;
    if (recoveringRunsRef.current.has(runId)) return;
    recoveringRunsRef.current.add(runId);
    const assistantId = `${runId}:assistant`;
    const controller = new AbortController();
    const alreadyElapsed = Math.max(0, Date.now() - new Date(run.context.started_at).getTime());
    const startedAt = performance.now() - alreadyElapsed;
    setRunning(true);
    setElapsedMs(alreadyElapsed);
    setStatusLabel("正在恢复未完成任务");
    setTraceTools(run.tool_events ?? []);
    setTraceEvidence(run.answer?.citations ?? []);
    abortRef.current = controller;
    activeRunRef.current = {
      runId,
      assistantId,
      cursor: 0,
      sessionId: run.context.session_id
    };
    try {
      await followRun(runId, assistantId, controller);
    } catch (error) {
      const aborted = error instanceof Error && error.name === "AbortError";
      markRunFailure(assistantId, error, startedAt, aborted);
    } finally {
      recoveringRunsRef.current.delete(runId);
      if (activeRunRef.current?.runId === runId) activeRunRef.current = undefined;
      if (abortRef.current === controller) abortRef.current = undefined;
      setRunning(false);
      setResumeCandidate(undefined);
      await loadConversation(run.context.session_id);
      await refresh();
    }
  }

  async function stopActiveRun() {
    const active = activeRunRef.current;
    stopRequestedRef.current = true;
    setStatusLabel("正在停止任务");
    if (!active) return;
    try {
      const cancelled = await api.cancelRun(active.runId);
      updateAssistant(active.assistantId, (message) => ({
        ...message,
        streaming: false,
        status: "已停止",
        phase: "executing",
        durationMs: cancelled.completed_at
          ? Math.max(
              0,
              new Date(cancelled.completed_at).getTime()
                - new Date(cancelled.context.started_at).getTime()
            )
          : elapsedMs,
        cancelled: true,
        retryable: true,
        error: "任务已停止。你可以修改请求后重新开始。"
      }));
      abortRef.current?.abort();
    } catch (error) {
      stopRequestedRef.current = false;
      setStatusLabel("停止失败，任务仍在运行");
      console.error("Unable to stop run", error);
    }
  }

  function selectRun(run: RunTrajectory) {
    setSelectedRun(run);
    setSelectedEvidence(undefined);
    setInspectorOpen(true);
  }

  function inspectEvidence(evidence: Evidence) {
    setSelectedEvidence(evidence);
    setSelectedRun(undefined);
    setInspectorOpen(true);
  }

  const changeView = useCallback((next: ViewName) => {
    setView(next);
    setSelectedEvidence(undefined);
    setInspectorOpen(false);
  }, []);

  const startNewConversation = useCallback(() => {
    if (running) return;
    const nextSessionId = createSessionId();
    conversationRequestRef.current += 1;
    loadedConversationSessionRef.current = nextSessionId;
    failedConversationRef.current = undefined;
    setSessionId(nextSessionId);
    setMessages([]);
    setTraceTools([]);
    setTraceEvidence([]);
    setSelectedEvidence(undefined);
    setSelectedRun(undefined);
    setResumeCandidate(undefined);
    setConversationLoading(false);
    setSessionError(undefined);
    setStatusLabel("Ready");
    setElapsedMs(0);
    setView("chat");
    setInspectorOpen(false);
  }, [running]);

  const selectConversation = useCallback((nextSessionId: string) => {
    if (running || nextSessionId === sessionId) return;
    void loadConversation(nextSessionId, { commitSession: true });
    setStatusLabel("Ready");
    setElapsedMs(0);
    setView("chat");
    setInspectorOpen(false);
  }, [loadConversation, running, sessionId]);

  const renameConversation = useCallback(async (targetSessionId: string, title: string) => {
    await api.updateConversation(targetSessionId, { title });
    await refresh();
  }, [refresh]);

  const setConversationArchived = useCallback(async (
    targetSessionId: string,
    archived: boolean
  ) => {
    await api.updateConversation(targetSessionId, { archived });
    if (archived && targetSessionId === sessionId) startNewConversation();
    await refresh();
  }, [refresh, sessionId, startNewConversation]);

  const openTask = useCallback((taskId: string) => {
    setFocusedTaskId(taskId);
    changeView("actions");
  }, [changeView]);

  const openReview = useCallback((date: string) => {
    setFocusedReviewDate(date);
    changeView("review");
  }, [changeView]);

  async function markReminderRead(taskId: string) {
    const feed = await api.markTaskReminderRead(taskId);
    setReminderFeed(feed);
  }

  async function markAllRemindersRead() {
    const feed = await api.markAllTaskRemindersRead();
    setReminderFeed(feed);
  }

  async function snoozeReminder(taskId: string) {
    const feed = await api.snoozeTaskReminder(taskId, 60);
    setReminderFeed(feed);
  }

  async function enableDesktopReminders() {
    if (!("Notification" in window)) return;
    const permission = await window.Notification.requestPermission();
    setDesktopPermission(permission);
    if (permission === "granted" && reminderFeed) emitDesktopReminders(reminderFeed);
  }

  async function authenticate(token: string) {
    setApiToken(token);
    setAuthError(undefined);
    try {
      const currentIdentity = await api.authMe();
      setIdentity(currentIdentity);
      setAuthRequired(false);
    } catch {
      setApiToken();
      setAuthRequired(true);
      setAuthError("访问令牌无效或已失效，请重新输入。");
    }
  }

  function signOut() {
    invalidateAsyncWorkspaceLoads();
    setApiToken();
    setIdentity(undefined);
    setAuthRequired(true);
    setAuthError(undefined);
  }

  const inspectorAvailable = view === "chat" || view === "runs";
  const showInspector = inspectorAvailable && (
    inspectorOpen || Boolean(selectedEvidence) || Boolean(selectedRun)
  );

  return (
    <>
    <div className="app-shell" aria-hidden={authChecking || authRequired ? true : undefined}>
      <Sidebar view={view} onViewChange={changeView} overview={overview} />
      <main className="main-area">
        <header className="topbar">
          <div
            className="topbar-title"
            aria-label={`${workspaceProfile?.display_name ?? "研发工作区"}，${viewTitles[view]}`}
          >
            <span>{workspaceProfile?.display_name ?? viewSections[view]}</span>
            <i>/</i>
            <strong>{viewTitles[view]}</strong>
          </div>
          <button className="command-trigger" onClick={() => setCommandOpen(true)}>
            <Search size={15} />
            <span>搜索或执行命令</span>
          </button>
          <div className="topbar-actions">
            <span className="learning-badge">
              <i />
              {learningModeLabel(overview?.learning_mode)}
            </span>
            <NotificationCenter
              feed={reminderFeed}
              refreshing={reminderRefreshing}
              desktopPermission={desktopPermission}
              onRefresh={refreshReminders}
              onMarkRead={markReminderRead}
              onMarkAllRead={markAllRemindersRead}
              onSnooze={snoozeReminder}
              onOpenTask={openTask}
              onEnableDesktop={enableDesktopReminders}
            />
            {identity?.auth_mode === "bearer" && (
              <button
                className="icon-button auth-signout-button"
                onClick={signOut}
                title={`退出 ${identity.user_id} 的 API 会话`}
              >
                <LogOut size={17} />
              </button>
            )}
            <button className="icon-button workspace-refresh-button" onClick={refresh} title="刷新工作区">
              <RefreshCw size={17} className={refreshing ? "spin" : ""} />
            </button>
            <a className="icon-button api-docs-button" href="/docs" target="_blank" title="API 文档">
              <ExternalLink size={17} />
            </a>
            {inspectorAvailable && (
              <button
                className="icon-button mobile-inspector-button"
                title="打开任务详情"
                onClick={() => setInspectorOpen(true)}
              >
                <PanelRight size={17} />
              </button>
            )}
          </div>
        </header>

        {workspaceError && (
          <div className="workspace-refresh-error" role="alert">
            <span>{workspaceError}</span>
            <button className="text-button" onClick={() => void refresh()} disabled={refreshing}>
              <RefreshCw size={14} className={refreshing ? "spin" : ""} />
              重试
            </button>
            <button
              className="icon-button"
              title="关闭刷新提示"
              onClick={() => setWorkspaceError(undefined)}
            >
              <X size={15} />
            </button>
          </div>
        )}
        {sessionError && (
          <div className="workspace-refresh-error" role="alert">
            <span>{sessionError}</span>
            <button
              className="text-button"
              onClick={() => {
                const failed = failedConversationRef.current;
                if (failed) void loadConversation(failed.sessionId, { commitSession: failed.commitSession });
              }}
              disabled={!failedConversationRef.current || conversationLoading}
            >
              <RefreshCw size={14} className={conversationLoading ? "spin" : ""} />
              重试
            </button>
            <button
              className="icon-button"
              title="关闭对话提示"
              onClick={() => {
                failedConversationRef.current = undefined;
                setSessionError(undefined);
              }}
            >
              <X size={15} />
            </button>
          </div>
        )}

        <div className={`content-grid ${showInspector ? "with-inspector" : ""}`}>
          {view === "chat" && (
            <ChatView
              key={sessionId}
              messages={messages}
              conversations={conversations}
              sessionId={sessionId}
              conversationLoading={conversationLoading}
              overview={overview}
              documents={documents}
              memories={memories}
              workspaceMode={workspaceProfile?.workspace_mode}
              sampleImportAvailable={sampleImportAvailable}
              running={running}
              statusLabel={statusLabel}
              elapsedMs={elapsedMs}
              domainPack={domainPack}
              onDomainPackChange={setDomainPack}
              onSubmit={submitTask}
              onStop={() => void stopActiveRun()}
              onNewConversation={startNewConversation}
              onConversationChange={selectConversation}
              onRenameConversation={renameConversation}
              onSetConversationArchived={setConversationArchived}
              onMemoryChanged={refresh}
              onWorkspaceChanged={refresh}
              onOpenTask={openTask}
              onOpenReview={openReview}
              onOpenKnowledge={() => changeView("knowledge")}
              onOpenMemory={() => changeView("memory")}
              onInspectEvidence={inspectEvidence}
            />
          )}
          <Suspense fallback={<ViewLoadingFallback />}>
          {view === "runs" && <RunsView runs={runs} onSelect={selectRun} />}
          {view === "knowledge" && (
            <KnowledgeView
              documents={documents}
              ingestionMode={overview?.ingestion_mode ?? "sync"}
              sampleImportAvailable={sampleImportAvailable}
              onChanged={refresh}
              onOpenChat={(suggestion) => {
                if (suggestion) {
                  window.localStorage.setItem(`hermesgraph:draft:${sessionId}`, suggestion);
                }
                changeView("chat");
              }}
            />
          )}
          {view === "graph" && <GraphView onChanged={refresh} canReview={identity?.role === "owner"} />}
          {view === "memory" && <MemoryView memories={memories} onChanged={refresh} />}
          {view === "actions" && (
            <ActionsView
              focusedTaskId={focusedTaskId}
              onChanged={() => void refreshReminders()}
            />
          )}
          {view === "review" && <ReviewView initialDate={focusedReviewDate} />}
          {view === "profile" && <ProfileView />}
          {view === "skills" && <SkillsView snapshots={skillEvolution} onChanged={refresh} />}
          {view === "learning" && (
            <LearningView
              changes={changes}
              memories={memories}
              skillEvolution={skillEvolution}
              onOpenMemory={() => changeView("memory")}
              onOpenSkills={() => changeView("skills")}
            />
          )}
          </Suspense>
          {showInspector && (
            <Inspector
              overview={overview}
              statusLabel={statusLabel}
              toolEvents={traceTools}
              evidence={traceEvidence}
              selectedEvidence={selectedEvidence}
              selectedRun={selectedRun}
              running={running}
              mobileOpen={inspectorOpen}
              onClose={() => { setInspectorOpen(false); setSelectedEvidence(undefined); setSelectedRun(undefined); }}
            />
          )}
        </div>
      </main>
      <CommandPalette
        open={commandOpen}
        onClose={() => setCommandOpen(false)}
        onNavigate={changeView}
        onRefresh={refresh}
        onNewConversation={startNewConversation}
        conversations={conversations}
        onConversationChange={selectConversation}
      />
      <Suspense fallback={null}>
        {persona && onboardingOpen && (
          <OnboardingDialog
            persona={persona}
            onLater={() => {
              window.localStorage.setItem(ONBOARDING_DISMISSED_KEY, String(Date.now()));
              setOnboardingOpen(false);
            }}
            onComplete={async (patch) => {
              const updated = await api.updatePersona(patch);
              setPersona(updated);
              setOnboardingOpen(false);
              window.localStorage.removeItem(ONBOARDING_DISMISSED_KEY);
              await refresh();
            }}
          />
        )}
      </Suspense>
    </div>
    <Suspense fallback={null}>
      {(authChecking || authRequired) && (
        <AuthDialog
          checking={authChecking}
          error={authError}
          onAuthenticate={authenticate}
        />
      )}
    </Suspense>
    </>
  );
}

export default App;

import { useEffect, useRef, useState } from "react";
import {
  Archive,
  ArrowUp,
  BookmarkPlus,
  Bot,
  BookOpenText,
  Check,
  ChevronDown,
  CircleStop,
  Copy,
  FileText,
  FlaskConical,
  GitCompareArrows,
  LoaderCircle,
  ListPlus,
  MessageSquareText,
  MoreHorizontal,
  Paperclip,
  Pencil,
  RotateCcw,
  SquarePen,
  ThumbsDown,
  ThumbsUp,
  UserRound,
  X
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api";
import { formatDuration } from "../runActivity";
import type {
  ConversationMessage,
  ConversationSummary,
  Evidence,
  KnowledgeDocument,
  Overview,
  SampleWorkspaceImport,
  WorkspaceMode
} from "../types";
import { QuickCapture, type QuickCaptureResult } from "./QuickCapture";
import { RunActivityTimeline } from "./RunActivityTimeline";

interface ChatViewProps {
  messages: ConversationMessage[];
  conversations: ConversationSummary[];
  sessionId: string;
  conversationLoading: boolean;
  overview?: Overview;
  documents: KnowledgeDocument[];
  workspaceMode?: WorkspaceMode;
  sampleImportAvailable: boolean;
  running: boolean;
  statusLabel: string;
  elapsedMs: number;
  domainPack: string;
  onDomainPackChange: (value: string) => void;
  onSubmit: (input: string, attachments?: string[]) => void;
  onStop: () => void;
  onNewConversation: () => void;
  onConversationChange: (sessionId: string) => void;
  onRenameConversation: (sessionId: string, title: string) => Promise<void>;
  onSetConversationArchived: (sessionId: string, archived: boolean) => Promise<void>;
  onMemoryChanged: () => Promise<void> | void;
  onWorkspaceChanged: () => Promise<void> | void;
  onOpenTask: (taskId: string) => void;
  onOpenReview: (date: string) => void;
  onOpenKnowledge: () => void;
  onInspectEvidence: (evidence: Evidence) => void;
}

type AttachmentStatus = "uploading" | "processing" | "ready" | "error";

interface ChatAttachment {
  id: string;
  file: File;
  status: AttachmentStatus;
  error?: string;
}

const MAX_ATTACHMENTS = 5;
const ATTACHMENT_ACCEPT = ".pdf,.md,.markdown,.txt,.json,.csv,.html,.htm,.png,.jpg,.jpeg,.webp";

function delay(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

const teamSuggestions = [
  {
    icon: GitCompareArrows,
    label: "服务依赖",
    text: "梳理 Atlas 平台的核心服务和依赖关系"
  },
  {
    icon: BookOpenText,
    label: "事故复盘",
    text: "Sentinel 密钥轮换事故的根因是什么？"
  },
  {
    icon: FlaskConical,
    label: "检索排障",
    text: "Polaris 检索变慢时应该先检查什么？"
  },
  {
    icon: ListPlus,
    label: "新人入职",
    text: "为新工程师生成第一周学习计划"
  }
];

const personalSuggestions = [
  {
    icon: BookOpenText,
    label: "论文理解",
    text: "帮我梳理这篇论文的核心方法、假设和局限"
  },
  {
    icon: GitCompareArrows,
    label: "知识比较",
    text: "比较我最近保存的两种技术方案，并给出选择依据"
  },
  {
    icon: FlaskConical,
    label: "学习回顾",
    text: "根据我的学习记录，列出本周最值得复习的三个主题"
  },
  {
    icon: ListPlus,
    label: "学习计划",
    text: "为我生成一个可执行的本周学习计划"
  }
];

const domainLabels: Record<string, string> = {
  general: "通用协作",
  research: "个人学习",
  research_reference: "个人学习",
  software_engineering: "团队研发",
  software_docs: "团队研发",
  software_docs_reference: "团队研发"
};

function sampleImportProgressLabel(sampleImport?: SampleWorkspaceImport) {
  if (!sampleImport) return "正在提交示例工作区导入。";
  const completed = Object.keys(sampleImport.completed_document_ids).length;
  const planned = sampleImport.plan.length;
  return planned > 0
    ? `正在导入示例资料：${completed}/${planned}。`
    : "正在等待示例工作区导入。";
}

function conversationLabel(conversation: ConversationSummary) {
  const title = conversation.title.length > 34
    ? `${conversation.title.slice(0, 34)}...`
    : conversation.title;
  const updated = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(conversation.updated_at));
  return `${title} · ${updated}${conversation.run_count > 1 ? ` · ${conversation.run_count} 轮` : ""}`;
}

function graphPathLabel(message: ConversationMessage) {
  const first = message.graphPaths?.[0];
  if (!first?.nodes?.length) return "";
  return first.nodes.map((node) => node.name).join(" -> ");
}

function taskSeedFromAnswer(content: string) {
  const line = content
    .replace(/^#{1,6}\s*/gm, "")
    .split("\n")
    .map((item) => item.trim())
    .find(Boolean) ?? "跟进本次研发结论";
  return {
    mode: "task" as const,
    title: `跟进：${line.slice(0, 56)}`,
    content: content.slice(0, 2_000)
  };
}

export function ChatView({
  messages,
  conversations,
  sessionId,
  conversationLoading,
  overview,
  documents,
  workspaceMode,
  sampleImportAvailable,
  running,
  statusLabel,
  elapsedMs,
  domainPack,
  onDomainPackChange,
  onSubmit,
  onStop,
  onNewConversation,
  onConversationChange,
  onRenameConversation,
  onSetConversationArchived,
  onMemoryChanged,
  onWorkspaceChanged,
  onOpenTask,
  onOpenReview,
  onOpenKnowledge,
  onInspectEvidence
}: ChatViewProps) {
  const draftKey = `hermesgraph:draft:${sessionId}`;
  const [input, setInput] = useState(
    () => window.localStorage.getItem(draftKey) ?? ""
  );
  const [copiedMessage, setCopiedMessage] = useState<string>();
  const [feedbackState, setFeedbackState] = useState<Record<string, "up" | "down" | "pending" | "error">>({});
  const [feedbackEditing, setFeedbackEditing] = useState<string>();
  const [feedbackNotes, setFeedbackNotes] = useState<Record<string, string>>({});
  const [memoryState, setMemoryState] = useState<Record<string, "pending" | "saved" | "error">>({});
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [captureOpen, setCaptureOpen] = useState(false);
  const [captureSeed, setCaptureSeed] = useState<{
    mode: "task" | "schedule" | "note";
    title: string;
    content: string;
  }>();
  const [captureResult, setCaptureResult] = useState<QuickCaptureResult>();
  const [sampleImport, setSampleImport] = useState<SampleWorkspaceImport>();
  const [sampleImportState, setSampleImportState] = useState<
    "idle" | "confirming" | "starting" | "unavailable" | "error"
  >("idle");
  const [sampleImportMessage, setSampleImportMessage] = useState<string>();
  const [expandedGraphPaths, setExpandedGraphPaths] = useState<Record<string, boolean>>({});
  const [expandedLimitations, setExpandedLimitations] = useState<Record<string, boolean>>({});
  const [conversationMenuOpen, setConversationMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameTitle, setRenameTitle] = useState("");
  const [conversationAction, setConversationAction] = useState<"rename" | "archive" | "restore">();
  const [conversationActionError, setConversationActionError] = useState<string>();
  const bottomRef = useRef<HTMLDivElement>(null);
  const attachmentInputRef = useRef<HTMLInputElement>(null);
  const conversationMenuRef = useRef<HTMLDivElement>(null);
  const mountedRef = useRef(true);
  const composerTextRef = useRef<HTMLTextAreaElement>(null);
  const activeConversations = conversations.filter((item) => !item.archived);
  const archivedConversations = conversations.filter((item) => item.archived);
  const currentConversation = conversations.find((item) => item.session_id === sessionId);

  useEffect(() => {
    if (input) window.localStorage.setItem(draftKey, input);
    else window.localStorage.removeItem(draftKey);
  }, [draftKey, input]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, statusLabel]);

  useEffect(() => {
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!conversationMenuOpen) return;
    function closeMenu(event: PointerEvent) {
      if (!conversationMenuRef.current?.contains(event.target as Node)) {
        setConversationMenuOpen(false);
        setRenaming(false);
      }
    }
    window.addEventListener("pointerdown", closeMenu);
    return () => window.removeEventListener("pointerdown", closeMenu);
  }, [conversationMenuOpen]);

  useEffect(() => {
    if (!sampleImport || !["queued", "running"].includes(sampleImport.status)) return;
    const timer = window.setInterval(() => {
      void api.enterpriseFixtureImportStatus(sampleImport.run_id).then((next) => {
        setSampleImport(next);
        if (next.status === "succeeded") {
          setSampleImportState("idle");
          setSampleImportMessage(
            "示例工作区已导入" +
              (Object.keys(next.completed_document_ids).length
                ? ` ${Object.keys(next.completed_document_ids).length} 份资料`
                : "") +
              "。"
          );
          void onWorkspaceChanged();
        }
        if (next.status === "failed") {
          setSampleImportState("error");
          setSampleImportMessage("示例工作区导入没有完成，请稍后重试。");
        }
      }).catch(() => {
        setSampleImportState("error");
        setSampleImportMessage("无法读取示例工作区导入进度，请稍后在知识页查看。");
      });
    }, 1_500);
    return () => window.clearInterval(timer);
  }, [onWorkspaceChanged, sampleImport]);

  function updateAttachment(id: string, patch: Partial<ChatAttachment>) {
    if (!mountedRef.current) return;
    setAttachments((current) =>
      current.map((attachment) =>
        attachment.id === id ? { ...attachment, ...patch } : attachment
      )
    );
  }

  async function uploadAttachment(attachment: ChatAttachment) {
    try {
      if (overview?.ingestion_mode === "async") {
        const submission = await api.submitIngestionJob(attachment.file);
        updateAttachment(attachment.id, { status: "processing" });
        let job = submission.job;
        for (let attempt = 0; attempt < 120; attempt += 1) {
          if (!mountedRef.current) return;
          if (job.status === "succeeded") {
            updateAttachment(attachment.id, { status: "ready" });
            await onWorkspaceChanged();
            return;
          }
          if (job.status === "failed" || job.status === "cancelled") {
            throw new Error("附件处理失败，请稍后在知识页查看任务状态。");
          }
          await delay(1_000);
          job = await api.ingestionJob(job.job_id);
        }
        throw new Error("附件处理时间较长，请稍后在知识库查看状态");
      }
      await api.uploadDocument(attachment.file);
      updateAttachment(attachment.id, { status: "ready" });
      await onWorkspaceChanged();
    } catch {
      updateAttachment(attachment.id, {
        status: "error",
        error: "附件上传没有完成，请检查文件或稍后再试。"
      });
    }
  }

  function addAttachments(files: FileList | File[]) {
    const existing = new Set(
      attachments.map((item) => `${item.file.name}:${item.file.size}:${item.file.lastModified}`)
    );
    const available = Math.max(0, MAX_ATTACHMENTS - attachments.length);
    const next = Array.from(files)
      .filter((file) => !existing.has(`${file.name}:${file.size}:${file.lastModified}`))
      .slice(0, available)
      .map((file) => ({
        id: crypto.randomUUID(),
        file,
        status: "uploading" as const
      }));
    if (next.length === 0) return;
    setAttachments((current) => [...current, ...next]);
    next.forEach((attachment) => void uploadAttachment(attachment));
  }

  function applySuggestion(text: string) {
    setInput(text);
    window.requestAnimationFrame(() => composerTextRef.current?.focus());
  }

  async function startSampleWorkspaceImport() {
    if (sampleImportState === "starting") return;
    if (!sampleImportAvailable) {
      setSampleImportState("unavailable");
      setSampleImportMessage("示例工作区导入服务尚未启用。请先上传团队资料。");
      return;
    }
    setSampleImportState("starting");
    setSampleImportMessage(undefined);
    try {
      const started = await api.startEnterpriseFixtureImport();
      setSampleImport(started);
      if (started.status === "succeeded") {
        setSampleImportState("idle");
        setSampleImportMessage("示例工作区已就绪，可以开始提问。");
        await onWorkspaceChanged();
      } else if (started.status === "failed") {
        setSampleImportState("error");
        setSampleImportMessage("示例工作区导入没有完成，请稍后重试。");
      }
    } catch {
      setSampleImportState("error");
      setSampleImportMessage("无法开始示例工作区导入，请稍后重试。");
    }
  }

  function submit() {
    const value = input.trim();
    const readyAttachments = attachments
      .filter((attachment) => attachment.status === "ready")
      .map((attachment) => attachment.file.name);
    const attachmentBlocked = attachments.some(
      (attachment) => attachment.status !== "ready"
    );
    if ((!value && readyAttachments.length === 0) || attachmentBlocked || running || conversationLoading) return;
    setInput("");
    setAttachments([]);
    onSubmit(value, readyAttachments);
  }

  async function saveConversationTitle() {
    const title = renameTitle.trim();
    if (!currentConversation || !title || conversationAction) return;
    setConversationAction("rename");
    setConversationActionError(undefined);
    try {
      await onRenameConversation(currentConversation.session_id, title);
      setRenaming(false);
      setConversationMenuOpen(false);
    } catch (reason) {
      setConversationActionError(reason instanceof Error ? reason.message : "重命名失败");
    } finally {
      setConversationAction(undefined);
    }
  }

  async function archiveCurrentConversation() {
    if (!currentConversation || conversationAction) return;
    setConversationAction("archive");
    setConversationActionError(undefined);
    try {
      await onSetConversationArchived(currentConversation.session_id, true);
      setConversationMenuOpen(false);
    } catch (reason) {
      setConversationActionError(reason instanceof Error ? reason.message : "归档失败");
    } finally {
      setConversationAction(undefined);
    }
  }

  async function restoreConversation(targetSessionId: string) {
    if (conversationAction) return;
    setConversationAction("restore");
    setConversationActionError(undefined);
    try {
      await onSetConversationArchived(targetSessionId, false);
    } catch (reason) {
      setConversationActionError(reason instanceof Error ? reason.message : "恢复失败");
    } finally {
      setConversationAction(undefined);
    }
  }

  async function rememberMessage(message: ConversationMessage) {
    if (!message.content.trim() || memoryState[message.id] === "pending") return;
    setMemoryState((current) => ({ ...current, [message.id]: "pending" }));
    try {
      await api.remember(message.content, sessionId);
      setMemoryState((current) => ({ ...current, [message.id]: "saved" }));
      await onMemoryChanged();
    } catch {
      setMemoryState((current) => ({ ...current, [message.id]: "error" }));
    }
  }

  async function submitFeedback(message: ConversationMessage, score: number) {
    if (!message.runId || feedbackState[message.id] === "pending") return;
    setFeedbackState((current) => ({ ...current, [message.id]: "pending" }));
    try {
      await api.feedback(message.runId, score, feedbackNotes[message.id]?.trim() || undefined);
      setFeedbackState((current) => ({
        ...current,
        [message.id]: score > 0 ? "up" : "down"
      }));
      setFeedbackEditing(undefined);
    } catch {
      setFeedbackState((current) => ({ ...current, [message.id]: "error" }));
    }
  }

  const attachmentBlocked = attachments.some(
    (attachment) => attachment.status !== "ready"
  );
  const canSubmit = Boolean(input.trim() || attachments.length > 0) && !attachmentBlocked;
  const isPersonalLearning = workspaceMode === "personal" || (
    !workspaceMode && ["research", "research_reference"].includes(domainPack)
  );
  const suggestedPrompts = isPersonalLearning ? personalSuggestions : teamSuggestions;
  const activeDocuments = documents.filter((document) => document.status === "active");
  const graphCandidateCount =
    (overview?.counts.graph_entity_candidates ?? 0) +
    (overview?.counts.graph_relation_candidates ?? 0);

  const composer = (
    <div className="composer-wrap">
      {running && (
        <div className="stream-status">
          <LoaderCircle size={14} className="spin" />
          <span>{statusLabel}</span>
          <time>{formatDuration(elapsedMs)}</time>
        </div>
      )}
      {captureOpen && (
        <QuickCapture
          initialMode={captureSeed?.mode}
          initialTitle={captureSeed?.title}
          initialContent={captureSeed?.content}
          onClose={() => setCaptureOpen(false)}
          onCreated={async (result) => {
            setCaptureResult(result);
            await onWorkspaceChanged();
          }}
        />
      )}
      {captureResult && !captureOpen && (
        <div className="quick-capture-result" role="status">
          <Check size={14} />
          <span>
            {captureResult.kind === "note" ? "笔记" : captureResult.kind === "schedule" ? "安排" : "任务"}
            “{captureResult.title}”已保存
          </span>
          <button
            className="text-button"
            onClick={() => {
              if (captureResult.kind === "note") onOpenReview(captureResult.date);
              else onOpenTask(captureResult.id);
            }}
          >
            查看
          </button>
          <button className="icon-button" title="关闭提示" onClick={() => setCaptureResult(undefined)}>
            <X size={13} />
          </button>
        </div>
      )}
      {attachments.length > 0 && (
        <div className="chat-attachments" aria-label="待发送附件">
          {attachments.map((attachment) => (
            <div
              className={`chat-attachment is-${attachment.status}`}
              key={attachment.id}
              title={attachment.error}
            >
              {attachment.status === "uploading" || attachment.status === "processing" ? (
                <LoaderCircle size={13} className="spin" />
              ) : attachment.status === "ready" ? (
                <Check size={13} />
              ) : (
                <FileText size={13} />
              )}
              <span>{attachment.file.name}</span>
              <small>
                {attachment.status === "uploading"
                  ? "上传中"
                  : attachment.status === "processing"
                    ? "解析中"
                    : attachment.status === "ready"
                      ? "可提问"
                      : "失败"}
              </small>
              <button
                title="移除附件"
                onClick={() =>
                  setAttachments((current) =>
                    current.filter((item) => item.id !== attachment.id)
                  )
                }
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
      <div
        className="composer"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          addAttachments(event.dataTransfer.files);
        }}
      >
        <input
          ref={attachmentInputRef}
          className="visually-hidden"
          type="file"
          accept={ATTACHMENT_ACCEPT}
          multiple
          onChange={(event) => {
            if (event.target.files) addAttachments(event.target.files);
            event.target.value = "";
          }}
        />
        <button
          className="composer-capture-button"
          title="快速记录"
          disabled={conversationLoading || running}
          aria-expanded={captureOpen}
          onClick={() => {
            setCaptureSeed(undefined);
            setCaptureOpen((current) => !current);
            setCaptureResult(undefined);
          }}
        >
          <ListPlus size={17} />
        </button>
        <button
          className="composer-attachment-button"
          title="添加附件"
          disabled={conversationLoading || running || attachments.length >= MAX_ATTACHMENTS}
          onClick={() => attachmentInputRef.current?.click()}
        >
          <Paperclip size={17} />
        </button>
        <textarea
          ref={composerTextRef}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !running) {
              event.preventDefault();
              submit();
            }
          }}
          placeholder="向 HermesGraph 提问，或交付一个任务"
          rows={2}
          disabled={conversationLoading}
        />
        <button
          className={`send-button ${running ? "is-stopping" : ""}`}
          onClick={running ? onStop : submit}
          title={running ? "停止" : "发送"}
          disabled={conversationLoading || (!running && !canSubmit)}
        >
          {running ? <CircleStop size={18} /> : <ArrowUp size={19} />}
        </button>
      </div>
    </div>
  );

  return (
    <section className="chat-view">
      <div className="chat-toolbar">
        <div className="segmented-control" aria-label="领域包">
          {(overview?.domain_packs ?? ["general", "research", "software_docs"]).map((pack) => (
            <button
              key={pack}
              className={domainPack === pack ? "is-selected" : ""}
              onClick={() => onDomainPackChange(pack)}
            >
              {domainLabels[pack] ?? pack}
            </button>
          ))}
        </div>
        <div className="chat-context-controls">
          <label className="conversation-picker" title="切换对话">
            <MessageSquareText size={14} />
            <select
              aria-label="当前对话"
              value={sessionId}
              disabled={running || conversationLoading}
              onChange={(event) => onConversationChange(event.target.value)}
            >
              {!activeConversations.some((item) => item.session_id === sessionId) && (
                <option value={sessionId}>新对话</option>
              )}
              {activeConversations.map((conversation) => (
                <option key={conversation.session_id} value={conversation.session_id}>
                  {conversationLabel(conversation)}
                </option>
              ))}
            </select>
            <ChevronDown size={13} />
          </label>
          <button
            className="icon-button new-chat-button"
            onClick={onNewConversation}
            title="新建对话"
            disabled={conversationLoading}
          >
            <SquarePen size={16} />
          </button>
          <div className="conversation-actions" ref={conversationMenuRef}>
            <button
              className="icon-button conversation-menu-button"
              title="管理对话"
              aria-expanded={conversationMenuOpen}
              onClick={() => {
                setConversationMenuOpen((current) => !current);
                setConversationActionError(undefined);
              }}
            >
              <MoreHorizontal size={17} />
            </button>
            {conversationMenuOpen && (
              <div className="conversation-menu" role="dialog" aria-label="管理对话">
                <div className="conversation-menu-heading">
                  <strong>当前对话</strong>
                  <span>{currentConversation ? `${currentConversation.run_count} 轮` : "尚未开始"}</span>
                </div>
                {renaming ? (
                  <div className="conversation-rename-form">
                    <input
                      autoFocus
                      value={renameTitle}
                      maxLength={200}
                      aria-label="对话标题"
                      onChange={(event) => setRenameTitle(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void saveConversationTitle();
                        if (event.key === "Escape") setRenaming(false);
                      }}
                    />
                    <button
                      title="保存标题"
                      disabled={!renameTitle.trim() || Boolean(conversationAction)}
                      onClick={() => void saveConversationTitle()}
                    >
                      {conversationAction === "rename" ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        <Check size={14} />
                      )}
                    </button>
                    <button title="取消" onClick={() => setRenaming(false)}>
                      <X size={14} />
                    </button>
                  </div>
                ) : (
                  <div className="conversation-menu-commands">
                    <button
                      disabled={!currentConversation || Boolean(conversationAction)}
                      onClick={() => {
                        setRenameTitle(currentConversation?.title ?? "");
                        setRenaming(true);
                      }}
                    >
                      <Pencil size={14} />
                      <span>重命名</span>
                    </button>
                    <button
                      disabled={!currentConversation || Boolean(conversationAction)}
                      onClick={() => void archiveCurrentConversation()}
                    >
                      {conversationAction === "archive" ? (
                        <LoaderCircle size={14} className="spin" />
                      ) : (
                        <Archive size={14} />
                      )}
                      <span>归档</span>
                    </button>
                  </div>
                )}
                {archivedConversations.length > 0 && (
                  <div className="archived-conversations">
                    <div className="conversation-menu-heading">
                      <strong>已归档</strong>
                      <span>{archivedConversations.length}</span>
                    </div>
                    {archivedConversations.map((conversation) => (
                      <div className="archived-conversation" key={conversation.session_id}>
                        <span title={conversation.title}>{conversation.title}</span>
                        <button
                          title="恢复对话"
                          disabled={Boolean(conversationAction)}
                          onClick={() => void restoreConversation(conversation.session_id)}
                        >
                          {conversationAction === "restore" ? (
                            <LoaderCircle size={13} className="spin" />
                          ) : (
                            <RotateCcw size={13} />
                          )}
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {conversationActionError && (
                  <div className="conversation-menu-error">{conversationActionError}</div>
                )}
              </div>
            )}
          </div>
          <div className="chat-workspace-mode" title="当前默认知识范围">
            <span className="status-indicator" />
            <span>{isPersonalLearning ? "个人学习" : "团队研发"}</span>
          </div>
        </div>
      </div>

      <div className="message-scroll">
        {conversationLoading ? (
          <div className="conversation-loading">
            <LoaderCircle size={18} className="spin" />
            <span>正在恢复对话</span>
          </div>
        ) : messages.length === 0 ? (
          <div className="empty-conversation">
            <div className="empty-status">
              <span />
              {activeDocuments.length > 0 ? "知识范围已就绪" : "等待添加知识资料"}
            </div>
            <div className="empty-heading">
              <div className="empty-icon">
                <Bot size={22} />
              </div>
              <div>
                <h1>{isPersonalLearning ? "从一个学习问题开始" : "从一个研发问题开始"}</h1>
                <p>{isPersonalLearning ? "个人资料与公开参考会按需参与回答。" : "查询架构、服务、事故或技术决策，并查看证据来源。"}</p>
              </div>
            </div>
            <div className="empty-composer-slot">{composer}</div>

            <div className="workspace-pulse knowledge-pulse" aria-label="当前知识状态">
              <div>
                <strong>{activeDocuments.length}</strong>
                <span>可检索资料</span>
              </div>
              <div>
                <strong>{overview?.counts.chunks ?? 0}</strong>
                <span>可用分块</span>
              </div>
              <div>
                <strong>{graphCandidateCount}</strong>
                <span>图谱候选</span>
              </div>
              <div>
                <strong>{overview?.counts.active_ingestion_jobs ?? 0}</strong>
                <span>处理中</span>
              </div>
            </div>
            <div className="knowledge-state-note">
              {activeDocuments.length > 0
                ? "资料状态来自当前工作区；回答会按问题需要检索。"
                : "还没有可检索资料。上传团队资料，或载入示例工作区后再开始提问。"}
            </div>

            <div className="empty-source-actions">
              <button className="text-button" onClick={onOpenKnowledge}>
                <Paperclip size={14} />
                {activeDocuments.length > 0 ? "查看知识资料" : "上传资料"}
              </button>
              {activeDocuments.length === 0 && (
                <button
                  className="text-button"
                  disabled={!sampleImportAvailable || sampleImportState === "starting"}
                  title={sampleImportAvailable ? "载入虚构研发资料" : "示例工作区导入服务尚未启用"}
                  onClick={() => setSampleImportState("confirming")}
                >
                  {sampleImportState === "starting" ? <LoaderCircle className="spin" size={14} /> : <BookOpenText size={14} />}
                  载入示例工作区
                </button>
              )}
            </div>
            {!sampleImportAvailable && activeDocuments.length === 0 && (
              <div className="sample-import-availability" role="status">
                示例工作区导入服务尚未启用；可先上传团队资料开始体验。
              </div>
            )}
            {sampleImportState === "confirming" && (
              <div className="sample-import-confirm" role="status">
                <div>
                  <strong>载入虚构研发资料</strong>
                  <span>将导入架构、服务、ADR、事故和 Runbook；重复执行应由服务端幂等处理。</span>
                </div>
                <div>
                  <button className="text-button" onClick={() => setSampleImportState("idle")}>取消</button>
                  <button className="primary-button" onClick={() => void startSampleWorkspaceImport()}>开始载入</button>
                </div>
              </div>
            )}
            {sampleImportState === "starting" && (
              <div className="sample-import-progress" role="status">
                <LoaderCircle className="spin" size={14} />
                <span>{sampleImportProgressLabel(sampleImport)}</span>
              </div>
            )}
            {sampleImportMessage && (
              <div className={`sample-import-message is-${sampleImportState}`} role="status">
                {sampleImportMessage}
              </div>
            )}

            <div className="suggestion-heading">
              <MessageSquareText size={15} />
              <span>可编辑的示例问题</span>
            </div>
            <div className="suggestion-list">
              {suggestedPrompts.map(({ icon: Icon, label, text }) => (
                <button key={text} onClick={() => applySuggestion(text)} disabled={running}>
                  <Icon size={17} />
                  <span>
                    <small>{label}</small>
                    <strong>{text}</strong>
                  </span>
                  <Pencil size={15} className="suggestion-arrow" />
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="messages">
            {messages.map((message, messageIndex) => (
              <article key={message.id} className={`message message-${message.role}`}>
                <div className="message-meta">
                  <span className="message-avatar">
                    {message.role === "user" ? <UserRound size={14} /> : <Bot size={14} />}
                  </span>
                  <strong>{message.role === "user" ? "你" : "HermesGraph"}</strong>
                  <time>
                    {message.createdAt.toLocaleTimeString("zh-CN", {
                      hour: "2-digit",
                      minute: "2-digit"
                    })}
                  </time>
                  {message.confidence &&
                    message.responseMode !== "conversational" &&
                    message.responseMode !== "action" && (
                    <span className={`confidence confidence-${message.confidence}`}>
                      {message.confidence}
                    </span>
                    )}
                </div>
                <div className="message-content">
                  {(message.attachments?.length ?? 0) > 0 && (
                    <div className="message-attachments">
                      {message.attachments?.map((attachment) => (
                        <span key={attachment}>
                          <FileText size={13} />
                          {attachment}
                        </span>
                      ))}
                    </div>
                  )}
                  {message.role === "assistant" ? (
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                  ) : (
                    <p>{message.content}</p>
                  )}
                  {message.role === "assistant" && !message.streaming && (
                    (message.citations?.length ?? 0) > 0 ||
                    (message.graphPaths?.length ?? 0) > 0 ||
                    (message.limitations?.length ?? 0) > 0
                  ) && (
                    <section className="answer-evidence-strip" aria-label="回答依据与限制">
                      {(message.citations?.length ?? 0) > 0 && (
                        <button
                          onClick={() => message.citations?.[0] && onInspectEvidence(message.citations[0])}
                          title="打开 Evidence Inspector"
                        >
                          <FileText size={14} />
                          {message.citations?.length} 个来源
                        </button>
                      )}
                      {(message.graphPaths?.length ?? 0) > 0 && (
                        <button
                          title={graphPathLabel(message)}
                          onClick={() => setExpandedGraphPaths((current) => ({
                            ...current,
                            [message.id]: !current[message.id]
                          }))}
                        >
                          <GitCompareArrows size={14} />
                          {message.graphPaths?.length} 条关系路径
                        </button>
                      )}
                      {(message.limitations?.length ?? 0) > 0 && (
                        <button
                          className="is-limitation"
                          onClick={() => setExpandedLimitations((current) => ({
                            ...current,
                            [message.id]: !current[message.id]
                          }))}
                        >
                          <FlaskConical size={14} />
                          {message.limitations?.length} 项限制
                        </button>
                      )}
                      {expandedGraphPaths[message.id] && (
                        <div className="answer-evidence-detail">
                          {message.graphPaths?.map((path, index) => (
                            <span key={`${message.id}:path:${index}`}>
                              {path.nodes.map((node) => node.name).join(" -> ")}
                            </span>
                          ))}
                        </div>
                      )}
                  {expandedLimitations[message.id] && (
                        <div className="answer-evidence-detail is-limitation">
                          {message.limitations?.map((limitation) => <span key={limitation}>{limitation}</span>)}
                        </div>
                      )}
                    </section>
                  )}
                  {message.role === "assistant" && !message.streaming && (
                    message.followUpActions?.length ?? 0
                  ) > 0 && (
                    <div className="followup-actions" aria-label="建议的下一步">
                      {message.followUpActions?.map((action) => (
                        <button
                          className="text-button"
                          key={action.action_id}
                          onClick={() => applySuggestion(action.query)}
                          title={action.query}
                        >
                          {action.label}
                        </button>
                      ))}
                    </div>
                  )}
                  {message.role === "assistant" && (
                    message.streaming ||
                    (message.toolEvents?.length ?? 0) > 0 ||
                    Boolean(message.error)
                  ) && (
                    <RunActivityTimeline
                      status={message.status ?? statusLabel}
                      streaming={message.streaming}
                      cancelled={message.cancelled}
                      error={message.error}
                      elapsedMs={message.streaming ? elapsedMs : message.durationMs}
                      toolEvents={message.toolEvents ?? []}
                    />
                  )}
                  {message.error && (
                    <div className="error-banner message-error">
                      <span>{message.error}</span>
                      {message.role === "assistant" &&
                        message.retryable !== false &&
                        messages[messageIndex - 1]?.role === "user" && (
                        <button
                          className="text-button"
                          disabled={running}
                          onClick={() =>
                            onSubmit(
                              messages[messageIndex - 1].content,
                              messages[messageIndex - 1].attachments
                            )
                          }
                        >
                          {message.cancelled ? "重新开始" : "重试任务"}
                        </button>
                      )}
                      {message.retryable === false && (
                        <small>请检查模型连接配置后再试</small>
                      )}
                    </div>
                  )}
                  {message.error && message.content && (
                    <div className="partial-answer-note">
                      已保留本次已生成的部分结果；请重试以获得完整回答。
                    </div>
                  )}
                </div>
                {message.role === "user" && message.content && (
                  <div className="message-footer message-user-actions">
                    <span />
                    <div className="feedback-actions">
                      <button
                        title="记住这条消息"
                        className={memoryState[message.id] === "saved" ? "is-selected" : ""}
                        disabled={memoryState[message.id] === "pending"}
                        onClick={() => void rememberMessage(message)}
                      >
                        {memoryState[message.id] === "saved" ? <Check size={15} /> : <BookmarkPlus size={15} />}
                      </button>
                      {memoryState[message.id] === "saved" && <span className="feedback-note">已记住</span>}
                      {memoryState[message.id] === "error" && <span className="feedback-note is-error">保存失败</span>}
                    </div>
                  </div>
                )}
                {message.role === "assistant" && !message.streaming && message.content && (
                  <div className="message-footer">
                    <div className="citation-buttons">
                      {(message.citations ?? []).map((evidence, index) => (
                        <button
                          key={evidence.evidence_id}
                          onClick={() => onInspectEvidence(evidence)}
                          title={evidence.title ?? evidence.provenance.source_id}
                        >
                          <FileText size={14} />
                          证据 {index + 1}
                        </button>
                      ))}
                      {message.durationMs !== undefined && (
                        <span className="run-completion-meta">
                          {formatDuration(message.durationMs)}
                          {(message.toolEvents?.length ?? 0) > 0 && (
                            <> · {message.toolEvents?.length} 个工具</>
                          )}
                          {(message.learningCount ?? 0) > 0 && (
                            <> · {message.learningCount} 项学习</>
                          )}
                        </span>
                      )}
                    </div>
                    {message.runId && (
                      <div className="feedback-actions">
                        <button
                          title="记住这条回答"
                          className={memoryState[message.id] === "saved" ? "is-selected" : ""}
                          disabled={memoryState[message.id] === "pending"}
                          onClick={() => void rememberMessage(message)}
                        >
                          {memoryState[message.id] === "saved" ? <Check size={15} /> : <BookmarkPlus size={15} />}
                        </button>
                        <button
                          title="根据回答生成任务"
                          onClick={() => {
                            setCaptureSeed(taskSeedFromAnswer(message.content));
                            setCaptureResult(undefined);
                            setCaptureOpen(true);
                            window.requestAnimationFrame(() => composerTextRef.current?.scrollIntoView({ behavior: "smooth", block: "center" }));
                          }}
                        >
                          <ListPlus size={15} />
                        </button>
                        <button
                          title="复制回答"
                          onClick={() => {
                            void navigator.clipboard.writeText(message.content);
                            setCopiedMessage(message.id);
                            window.setTimeout(() => setCopiedMessage(undefined), 1500);
                          }}
                        >
                          {copiedMessage === message.id ? <Check size={15} /> : <Copy size={15} />}
                        </button>
                        <button
                          title="有帮助"
                          className={(feedbackState[message.id] === "up" || (!feedbackState[message.id] && message.feedbackScore === 1)) ? "is-selected" : ""}
                          aria-pressed={feedbackState[message.id] === "up" || (!feedbackState[message.id] && message.feedbackScore === 1)}
                          disabled={feedbackState[message.id] === "pending"}
                          onClick={() => void submitFeedback(message, 1)}
                        >
                          <ThumbsUp size={15} />
                        </button>
                        <button
                          title="需要改进"
                          className={(feedbackState[message.id] === "down" || (!feedbackState[message.id] && message.feedbackScore === -1)) ? "is-selected" : ""}
                          aria-pressed={feedbackState[message.id] === "down" || (!feedbackState[message.id] && message.feedbackScore === -1)}
                          disabled={feedbackState[message.id] === "pending"}
                          onClick={() => setFeedbackEditing((current) => current === message.id ? undefined : message.id)}
                        >
                          <ThumbsDown size={15} />
                        </button>
                        {feedbackState[message.id] === "error" && <span className="feedback-note is-error">提交失败</span>}
                        {(feedbackState[message.id] === "up" || feedbackState[message.id] === "down") && <span className="feedback-note">已记录</span>}
                        {memoryState[message.id] === "saved" && <span className="feedback-note">已记住</span>}
                        {memoryState[message.id] === "error" && <span className="feedback-note is-error">保存失败</span>}
                      </div>
                    )}
                  </div>
                )}
                {message.role === "assistant" && feedbackEditing === message.id && (
                  <form
                    className="feedback-editor"
                    onSubmit={(event) => {
                      event.preventDefault();
                      void submitFeedback(message, -1);
                    }}
                  >
                    <input
                      autoFocus
                      value={feedbackNotes[message.id] ?? ""}
                      onChange={(event) => setFeedbackNotes((current) => ({
                        ...current,
                        [message.id]: event.target.value
                      }))}
                      placeholder="哪里需要改进？（可选）"
                      maxLength={2000}
                    />
                    <button type="button" className="text-button" onClick={() => setFeedbackEditing(undefined)}>
                      取消
                    </button>
                    <button className="primary-button" disabled={feedbackState[message.id] === "pending"}>
                      提交
                    </button>
                  </form>
                )}
              </article>
            ))}
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {messages.length > 0 && composer}
    </section>
  );
}

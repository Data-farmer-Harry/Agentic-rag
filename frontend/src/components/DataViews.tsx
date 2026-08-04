import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import {
  Archive,
  Activity,
  ArrowRight,
  BookOpenText,
  Check,
  CheckCircle2,
  ChevronRight,
  Database,
  ExternalLink,
  FileText,
  FlaskConical,
  GitCommitHorizontal,
  GitMerge,
  LoaderCircle,
  ListChecks,
  Image as ImageIcon,
  Network,
  RefreshCw,
  Rocket,
  Search,
  ShieldAlert,
  Sparkles,
  Trash2,
  Upload,
  X
} from "lucide-react";
import { api } from "../api";
import type {
  GraphResult,
  GraphCandidateCollection,
  GraphCandidateStatus,
  GraphNode,
  GraphRelationship,
  IngestionJob,
  KnowledgeDocument,
  SampleWorkspaceImport,
  LearningChange,
  MemoryCorrectionResult,
  MemoryRecord,
  RunTrajectory,
  SkillDefinition,
  SkillEvolutionSnapshot
} from "../types";

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function formatBytes(value: number) {
  if (value < 1_000) return `${value} B`;
  if (value < 1_000_000) return `${(value / 1_000).toFixed(1)} KB`;
  return `${(value / 1_000_000).toFixed(1)} MB`;
}

type SourceLayerLabel = "团队内部" | "个人资料" | "公共参考" | "未标注";

function readMetadataText(metadata: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function documentSourceLayer(document: KnowledgeDocument): SourceLayerLabel {
  const value = readMetadataText(document.metadata, "source_layer", "knowledge_layer", "layer");
  if (value === "team_internal" || value === "team") return "团队内部";
  if (value === "personal") return "个人资料";
  if (value === "public_reference" || value === "public") return "公共参考";
  return "未标注";
}

function documentGraphStatus(document: KnowledgeDocument) {
  const status = readMetadataText(
    document.metadata,
    "graph_status",
    "graph_extraction_status",
    "knowledge_graph_status"
  );
  if (!status) return "未标注";
  const labels: Record<string, string> = {
    pending: "待抽取",
    candidate: "候选待审",
    approved: "已审核",
    ready: "可查询",
    not_applicable: "无需图谱",
    failed: "抽取失败"
  };
  return labels[status] ?? status;
}

function documentStatusLabel(status: KnowledgeDocument["status"]) {
  return status === "active" ? "可检索" : status === "archived" ? "已归档" : "失败";
}

function sampleImportProgressLabel(sampleImport?: SampleWorkspaceImport) {
  if (!sampleImport) return "正在提交示例工作区导入。";
  const completed = Object.keys(sampleImport.completed_document_ids).length;
  const planned = sampleImport.plan.length;
  return planned > 0
    ? `正在导入示例资料：${completed}/${planned}。`
    : "正在等待示例工作区导入。";
}

async function openDocument(document: KnowledgeDocument) {
  const blob = await api.documentContent(document.document_id);
  const objectUrl = window.URL.createObjectURL(blob);
  const anchor = window.document.createElement("a");
  anchor.href = objectUrl;
  anchor.target = "_blank";
  anchor.rel = "noreferrer";
  anchor.click();
  window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 60_000);
}

const activeIngestionStatuses = new Set(["queued", "running", "retry_scheduled"]);
const ingestionStatusLabels: Record<IngestionJob["status"], string> = {
  queued: "等待处理",
  running: "处理中",
  retry_scheduled: "等待重试",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

function EmptyState({ icon: Icon, label }: { icon: typeof Database; label: string }) {
  return (
    <div className="data-empty">
      <Icon size={22} />
      <span>{label}</span>
    </div>
  );
}

export function RunsView({
  runs,
  onSelect
}: {
  runs: RunTrajectory[];
  onSelect: (run: RunTrajectory) => void;
}) {
  return (
    <section className="data-view">
      <header className="view-header">
        <div><span className="eyebrow">Execution history</span><h1>运行记录</h1></div>
        <span className="view-count">{runs.length} runs</span>
      </header>
      {runs.length === 0 ? <EmptyState icon={CheckCircle2} label="暂无运行记录" /> : (
        <div className="data-table runs-table">
          <div className="table-head">
            <span>任务</span><span>领域</span><span>状态</span><span>证据</span><span>时间</span><span />
          </div>
          {runs.map((run) => (
            <button className="table-row" key={run.context.run_id} onClick={() => onSelect(run)}>
              <span className="primary-cell"><strong>{run.user_input}</strong><small>{run.context.run_id.slice(0, 8)}</small></span>
              <span>{run.context.domain_pack}</span>
              <span><i className={`status-dot status-${run.status}`} />{run.status}</span>
              <span>{run.answer?.citations.length ?? 0}</span>
              <span>{formatDate(run.context.started_at)}</span>
              <ChevronRight size={16} />
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

export function KnowledgeView({
  documents,
  ingestionMode,
  sampleImportAvailable,
  onChanged,
  onOpenChat
}: {
  documents: KnowledgeDocument[];
  ingestionMode: "sync" | "async";
  sampleImportAvailable: boolean;
  onChanged: () => Promise<void> | void;
  onOpenChat: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [confirmArchive, setConfirmArchive] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [error, setError] = useState<string>();
  const [jobs, setJobs] = useState<IngestionJob[]>([]);
  const [busyJob, setBusyJob] = useState<string>();
  const [sourceFilter, setSourceFilter] = useState<SourceLayerLabel | "all">("all");
  const [query, setQuery] = useState("");
  const [sampleImport, setSampleImport] = useState<SampleWorkspaceImport>();
  const [sampleImportState, setSampleImportState] = useState<
    "idle" | "confirming" | "starting" | "unavailable" | "error"
  >("idle");
  const [sampleImportMessage, setSampleImportMessage] = useState<string>();
  const knownJobStatuses = useRef(new Map<string, IngestionJob["status"]>());

  const loadJobs = useCallback(async () => {
    if (ingestionMode !== "async") return;
    try {
      const next = await api.ingestionJobs();
      const completed = next.filter(
        (job) =>
          job.status === "succeeded" &&
          knownJobStatuses.current.has(job.job_id) &&
          knownJobStatuses.current.get(job.job_id) !== "succeeded"
      );
      next.forEach((job) => knownJobStatuses.current.set(job.job_id, job.status));
      setJobs(next);
      if (completed.length > 0) {
        const latest = completed[0];
        setNotice(
          latest.deduplicated
            ? `${latest.filename} 已复用现有索引`
            : `${latest.filename} 已完成入库`
        );
        await onChanged();
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法刷新入库任务");
    }
  }, [ingestionMode, onChanged]);

  useEffect(() => {
    if (ingestionMode !== "async") {
      setJobs([]);
      knownJobStatuses.current.clear();
      return;
    }
    void loadJobs();
    const timer = window.setInterval(() => void loadJobs(), 1_500);
    return () => window.clearInterval(timer);
  }, [ingestionMode, loadJobs]);

  useEffect(() => {
    if (!sampleImport || !["queued", "running"].includes(sampleImport.status)) return;
    const timer = window.setInterval(() => {
      void api.enterpriseFixtureImportStatus(sampleImport.run_id).then((next) => {
        setSampleImport(next);
        if (next.status === "succeeded") {
          setSampleImportState("idle");
          setSampleImportMessage(
            Object.keys(next.completed_document_ids).length
              ? `示例工作区已完成导入 ${Object.keys(next.completed_document_ids).length} 份资料。`
              : "示例工作区已完成导入。"
          );
          void onChanged();
        }
        if (next.status === "failed") {
          setSampleImportState("error");
          setSampleImportMessage("示例工作区导入没有完成，请稍后重试。");
        }
      }).catch(() => {
        setSampleImportState("error");
        setSampleImportMessage("无法读取示例工作区导入进度。 ");
      });
    }, 1_500);
    return () => window.clearInterval(timer);
  }, [onChanged, sampleImport]);

  async function upload(file?: File) {
    if (!file || uploading) return;
    setUploading(true);
    setNotice(undefined);
    setError(undefined);
    try {
      if (ingestionMode === "async") {
        const submission = await api.submitIngestionJob(file);
        knownJobStatuses.current.set(submission.job.job_id, submission.job.status);
        setJobs((current) => [
          submission.job,
          ...current.filter((item) => item.job_id !== submission.job.job_id)
        ]);
        setNotice(
          submission.coalesced
            ? `${submission.job.filename} 已在任务队列中`
            : `${submission.job.filename} 已提交处理`
        );
      } else {
        const result = await api.uploadDocument(file);
        setNotice(
          result.deduplicated
            ? `${result.document.filename} 已存在，已复用原索引`
            : `${result.document.filename} 已入库，共 ${result.document.chunk_count} 个分块`
        );
        await onChanged();
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文档上传失败");
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void upload(event.dataTransfer.files[0]);
  }

  async function archive(documentId: string) {
    setError(undefined);
    try {
      await api.archiveDocument(documentId);
      setNotice("文档已归档，并已从后续检索中移除");
      setConfirmArchive(undefined);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "文档归档失败");
    }
  }

  async function cancelJob(jobId: string) {
    setBusyJob(jobId);
    setError(undefined);
    try {
      const cancelled = await api.cancelIngestionJob(jobId);
      knownJobStatuses.current.set(jobId, cancelled.status);
      setJobs((current) => current.map((item) => (item.job_id === jobId ? cancelled : item)));
      setNotice(`${cancelled.filename} 已取消`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "取消任务失败");
    } finally {
      setBusyJob(undefined);
    }
  }

  async function retryJob(jobId: string) {
    setBusyJob(jobId);
    setError(undefined);
    try {
      const retried = await api.retryIngestionJob(jobId);
      knownJobStatuses.current.set(jobId, retried.status);
      setJobs((current) => current.map((item) => (item.job_id === jobId ? retried : item)));
      setNotice(`${retried.filename} 已重新加入队列`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "重试任务失败");
    } finally {
      setBusyJob(undefined);
    }
  }

  async function startSampleImport() {
    if (sampleImportState === "starting") return;
    if (!sampleImportAvailable) {
      setSampleImportState("unavailable");
      setSampleImportMessage("示例工作区导入服务尚未启用。你可以继续上传自己的资料。");
      return;
    }
    setSampleImportState("starting");
    setSampleImportMessage(undefined);
    try {
      const started = await api.startEnterpriseFixtureImport();
      setSampleImport(started);
      if (started.status === "succeeded") {
        setSampleImportState("idle");
        setSampleImportMessage("示例工作区已就绪。");
        await onChanged();
      } else if (started.status === "failed") {
        setSampleImportState("error");
        setSampleImportMessage("示例工作区导入没有完成，请稍后重试。");
      }
    } catch {
      setSampleImportState("error");
      setSampleImportMessage("无法开始示例工作区导入，请稍后重试。");
    }
  }

  const activeCount = documents.filter((item) => item.status === "active").length;
  const visibleDocuments = documents.filter((document) => {
    const layerMatches = sourceFilter === "all" || documentSourceLayer(document) === sourceFilter;
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const queryMatches = !normalizedQuery ||
      `${document.title} ${document.filename}`.toLocaleLowerCase().includes(normalizedQuery);
    return layerMatches && queryMatches;
  });

  return (
    <section className="data-view knowledge-view">
      <header className="view-header">
        <div><span className="eyebrow">团队、个人与公共参考</span><h1>知识</h1></div>
        <span className="view-count">{activeCount} 份可检索 / {documents.length} 份总计</span>
      </header>
      <div
        className={`knowledge-command-bar ${dragging ? "is-dragging" : ""}`}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
        onDrop={drop}
      >
        <button className="primary-button" onClick={() => inputRef.current?.click()} disabled={uploading}>
          {uploading ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
          上传
        </button>
        <button
          className="text-button knowledge-sample-button"
          disabled={!sampleImportAvailable || sampleImportState === "starting"}
          title={sampleImportAvailable ? "载入虚构研发资料" : "示例工作区导入服务尚未启用"}
          onClick={() => setSampleImportState("confirming")}
        >
          <BookOpenText size={15} />
          载入示例
        </button>
        <label className="knowledge-filter">
          <span>来源</span>
          <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value as SourceLayerLabel | "all")}>
            <option value="all">全部来源</option>
            <option value="团队内部">团队内部</option>
            <option value="个人资料">个人资料</option>
            <option value="公共参考">公共参考</option>
            <option value="未标注">未标注</option>
          </select>
        </label>
        <label className="knowledge-search-input">
          <Search size={15} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索资料" />
        </label>
        <span className="knowledge-drop-hint">拖放文件也可以上传</span>
        <input
          ref={inputRef}
          className="visually-hidden"
          type="file"
          accept=".pdf,.png,.jpg,.jpeg,.webp,.md,.markdown,.txt,.json,.csv,.html,.htm"
          onChange={(event) => void upload(event.target.files?.[0])}
        />
      </div>
      {!sampleImportAvailable && (
        <div className="sample-import-availability" role="status">
          示例工作区导入服务尚未启用；可以直接上传团队或个人资料。
        </div>
      )}
      {sampleImportState === "confirming" && (
        <div className="sample-import-confirm knowledge-sample-confirm" role="status">
          <div>
            <strong>载入虚构研发资料</strong>
            <span>包含架构、服务、ADR、事故和 Runbook；导入进度会在这里持续更新。</span>
          </div>
          <div>
            <button className="text-button" onClick={() => setSampleImportState("idle")}>取消</button>
            <button className="primary-button" onClick={() => void startSampleImport()}>开始载入</button>
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
        <div className={`sample-import-message is-${sampleImportState}`} role="status">{sampleImportMessage}</div>
      )}
      {notice && <div className="notice-banner">{notice}</div>}
      {error && <div className="error-banner">{error}</div>}
      {ingestionMode === "async" && jobs.length > 0 && (
        <section className="ingestion-queue" aria-label="入库任务">
          <header>
            <div><ListChecks size={15} /><strong>入库任务</strong></div>
            <span>{jobs.filter((job) => activeIngestionStatuses.has(job.status)).length} 进行中</span>
          </header>
          <div className="ingestion-job-list">
            {jobs.slice(0, 10).map((job) => (
              <article className={`ingestion-job is-${job.status}`} key={job.job_id}>
                <div className="ingestion-job-primary">
                  <span className="ingestion-job-icon">
                    {job.status === "running" || job.status === "retry_scheduled" ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : job.status === "succeeded" ? (
                      <CheckCircle2 size={15} />
                    ) : job.status === "failed" ? (
                      <ShieldAlert size={15} />
                    ) : (
                      <FileText size={15} />
                    )}
                  </span>
                  <div>
                    <strong>{job.filename}</strong>
                    <small>
                      {job.error_message
                        ? "该入库任务没有完成，请重试或查看运行记录。"
                        : `${formatBytes(job.byte_size)} · ${job.content_hash.slice(0, 10)}`}
                    </small>
                  </div>
                </div>
                <span className={`ingestion-job-status status-${job.status}`}>
                  {ingestionStatusLabels[job.status]}
                </span>
                <span className="ingestion-attempt">{job.attempt}/{job.max_attempts}</span>
                <span className="ingestion-time">{formatDate(job.updated_at)}</span>
                <div className="ingestion-job-actions">
                  {(job.status === "queued" || job.status === "retry_scheduled") && (
                    <button
                      className="icon-button danger"
                      title="取消入库任务"
                      onClick={() => void cancelJob(job.job_id)}
                      disabled={busyJob === job.job_id}
                    >
                      <X size={14} />
                    </button>
                  )}
                  {job.can_retry && (job.status === "failed" || job.status === "cancelled") && (
                    <button
                      className="icon-button"
                      title="重新执行入库任务"
                      onClick={() => void retryJob(job.job_id)}
                      disabled={busyJob === job.job_id}
                    >
                      <RefreshCw size={14} />
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        </section>
      )}
      {documents.length === 0 ? (
        <div className="knowledge-empty-state">
          <EmptyState icon={FileText} label="还没有资料可参与检索" />
          <button className="text-button" onClick={onOpenChat}>返回对话</button>
        </div>
      ) : visibleDocuments.length === 0 ? (
        <div className="knowledge-empty-state">
          <EmptyState icon={Search} label="当前筛选没有匹配资料" />
          <button className="text-button" onClick={() => { setSourceFilter("all"); setQuery(""); }}>清除筛选</button>
        </div>
      ) : (
        <div className="document-list">
          <div className="document-head">
            <span>资料</span><span>来源层</span><span>状态</span><span>分块</span><span>大小</span><span>图谱</span><span>更新时间</span><span />
          </div>
          {visibleDocuments.map((document) => (
            <article className={`document-row is-${document.status}`} key={document.document_id}>
              <div className="document-primary">
                <span className="document-icon">
                  {document.media_type.startsWith("image/") ? (
                    <ImageIcon size={17} />
                  ) : (
                    <FileText size={17} />
                  )}
                </span>
                <div><strong>{document.filename}</strong><code>{document.content_hash.slice(0, 12)}</code></div>
              </div>
              <span className={`source-layer source-${documentSourceLayer(document)}`}>{documentSourceLayer(document)}</span>
              <span className={`document-status status-${document.status}`}>{documentStatusLabel(document.status)}</span>
              <span>{document.chunk_count}</span>
              <span>{formatBytes(document.byte_size)}</span>
              <span className={`document-graph-status graph-${documentGraphStatus(document)}`}>{documentGraphStatus(document)}</span>
              <span>{formatDate(document.updated_at)}</span>
              <div className="document-actions">
                <button
                  className="icon-button"
                  onClick={() => void openDocument(document)}
                  title={document.media_type.startsWith("image/") ? "查看原图" : "查看原文件"}
                >
                  <ExternalLink size={15} />
                </button>
                {document.status === "active" && confirmArchive !== document.document_id && (
                  <button className="icon-button danger" title="归档文档" onClick={() => setConfirmArchive(document.document_id)}><Archive size={16} /></button>
                )}
                {confirmArchive === document.document_id && (
                  <div className="archive-confirm">
                    <button onClick={() => setConfirmArchive(undefined)}>取消</button>
                    <button className="is-danger" onClick={() => void archive(document.document_id)}>确认</button>
                  </div>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export function MemoryView({
  memories,
  onChanged
}: {
  memories: MemoryRecord[];
  onChanged: () => void;
}) {
  const [showRevoked, setShowRevoked] = useState(false);
  const [correctionRequest, setCorrectionRequest] = useState("");
  const [correctionResult, setCorrectionResult] = useState<MemoryCorrectionResult>();
  const [selectedCandidates, setSelectedCandidates] = useState<string[]>([]);
  const [correcting, setCorrecting] = useState(false);
  const visible = showRevoked ? memories : memories.filter((item) => !item.revoked_at);

  async function revoke(memoryId: string) {
    await api.revokeMemory(memoryId);
    onChanged();
  }

  async function correct(confirmMemoryIds: string[] = []) {
    const request = correctionRequest.trim();
    if (!request) return;
    setCorrecting(true);
    try {
      const result = await api.correctMemory(request, confirmMemoryIds);
      setCorrectionResult(result);
      setSelectedCandidates([]);
      if (result.status === "applied") {
        setCorrectionRequest("");
        onChanged();
      }
    } finally {
      setCorrecting(false);
    }
  }

  return (
    <section className="data-view">
      <header className="view-header">
        <div><span className="eyebrow">Long-term context</span><h1>记忆库</h1></div>
        <label className="toggle-control">
          <input type="checkbox" checked={showRevoked} onChange={(event) => setShowRevoked(event.target.checked)} />
          <span />
          显示已撤回
        </label>
      </header>
      <form
        className="memory-correction"
        onSubmit={(event) => {
          event.preventDefault();
          void correct();
        }}
      >
        <div className="memory-correction-input">
          <Sparkles size={16} />
          <input
            value={correctionRequest}
            onChange={(event) => {
              setCorrectionRequest(event.target.value);
              setCorrectionResult(undefined);
            }}
            placeholder="例如：把“我偏好 Java”更正为“我现在主要使用 Python”"
          />
          <button
            className="primary-button"
            disabled={correcting || !correctionRequest.trim()}
          >
            {correcting ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
            处理
          </button>
        </div>
        {correctionResult && (
          <div className={`correction-result status-${correctionResult.status}`}>
            <span>{correctionResult.message}</span>
            {correctionResult.status === "needs_confirmation" && (
              <>
                <div className="correction-candidates">
                  {correctionResult.candidates.map((candidate) => (
                    <label key={candidate.memory_id}>
                      <input
                        type="checkbox"
                        checked={selectedCandidates.includes(candidate.memory_id)}
                        onChange={(event) =>
                          setSelectedCandidates((current) =>
                            event.target.checked
                              ? [...current, candidate.memory_id]
                              : current.filter((id) => id !== candidate.memory_id)
                          )
                        }
                      />
                      <span>{candidate.summary}</span>
                    </label>
                  ))}
                </div>
                <button
                  type="button"
                  className="text-button"
                  disabled={selectedCandidates.length === 0 || correcting}
                  onClick={() => void correct(selectedCandidates)}
                >
                  <CheckCircle2 size={14} />
                  确认更正
                </button>
              </>
            )}
          </div>
        )}
      </form>
      {visible.length === 0 ? <EmptyState icon={Database} label="暂无长期记忆" /> : (
        <div className="memory-list">
          {visible.map((memory) => (
            <article className={`memory-row ${memory.revoked_at ? "is-revoked" : ""}`} key={memory.memory_id}>
              <div className={`memory-type type-${memory.memory_type}`}><Database size={17} /></div>
              <div className="memory-body">
                <div className="row-title"><strong>{memory.summary}</strong><span>{memory.memory_type}</span></div>
                <div className="memory-meta">
                  <span>confidence {Math.round(memory.confidence * 100)}%</span>
                  <span>{memory.provenance[0]?.source_type}</span>
                  <span>{formatDate(memory.updated_at)}</span>
                </div>
              </div>
              {!memory.revoked_at && (
                <button className="icon-button danger" title="撤回记忆" onClick={() => revoke(memory.memory_id)}><Trash2 size={16} /></button>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export function SkillsView({
  snapshots,
  onChanged
}: {
  snapshots: SkillEvolutionSnapshot[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();

  async function evaluate(skill: SkillDefinition) {
    setBusy(skill.skill_id);
    setError(undefined);
    try {
      await api.evaluateSkill(skill.skill_id);
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "评测失败");
    } finally {
      setBusy(undefined);
    }
  }

  async function promote(skill: SkillDefinition, target: "canary" | "active") {
    setBusy(skill.skill_id);
    setError(undefined);
    try {
      const decision = await api.transitionSkill(skill.skill_id, target, true);
      if (!decision.allowed) throw new Error(decision.reasons.join(" · "));
      onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "晋级失败");
    } finally {
      setBusy(undefined);
    }
  }

  const statusLabels: Record<string, string> = {
    draft: "草稿",
    security_review: "安全审查",
    offline_pass: "离线通过",
    shadow: "影子观测",
    canary: "小流量",
    active: "已启用",
    rolled_back: "已回滚",
    deprecated: "已停用"
  };

  return (
    <section className="data-view">
      <header className="view-header">
        <div><span className="eyebrow">Progressive skills</span><h1>技能注册表</h1></div>
        <span className="view-count">{snapshots.length} versions</span>
      </header>
      {error && <div className="inline-error">{error}</div>}
      {snapshots.length === 0 ? <EmptyState icon={Sparkles} label="尚未挖掘出技能" /> : (
        <div className="skill-grid">
          {snapshots.map(({ skill, latest_evaluation: evaluation, health }) => (
            <article className="skill-item" key={`${skill.skill_id}-${skill.version}`}>
              <div className="skill-heading">
                <div className="skill-icon"><Sparkles size={18} /></div>
                <div><strong>{skill.name}</strong><span>v{skill.version}</span></div>
                <span className={`skill-status status-${skill.status}`}>{statusLabels[skill.status] ?? skill.status}</span>
              </div>
              <p>{skill.description}</p>
              <div className="step-list">
                {skill.steps.map((step, index) => (
                  <div key={`${step.action}-${index}`}><span>{index + 1}</span><code>{step.action}</code><small>{step.purpose}</small></div>
                ))}
              </div>
              {evaluation && (
                <div className="skill-evolution-metrics">
                  <span><FlaskConical size={13} />评测 {evaluation.passed_cases}/{evaluation.case_count}</span>
                  <span>质量 {Math.round(evaluation.baseline_score * 100)} → {Math.round(evaluation.candidate_score * 100)}</span>
                  <span className={evaluation.security_passed && evaluation.regression_passed ? "metric-pass" : "metric-fail"}>
                    {evaluation.security_passed && evaluation.regression_passed ? "门禁通过" : "门禁阻断"}
                  </span>
                </div>
              )}
              {health && (
                <div className="skill-health">
                  <div><span><Activity size={13} />{health.cohort === "shadow" ? "影子样本" : "激活样本"}</span><strong>{health.evaluated_observations}</strong></div>
                  <div className="health-track"><span style={{ width: `${Math.min(100, health.evaluated_observations / health.required_observations * 100)}%` }} /></div>
                  <small>{
                    health.promotion_evidence.recommended_action === "rollback"
                      ? "健康门禁已触发自动回滚"
                      : health.promotion_evidence.recommended_action === "rollback_recommended"
                        ? `检测到负反馈，建议回滚 (${health.promotion_evidence.negative_feedback_count})`
                        : health.promotion_ready
                          ? "量化证据已达到晋级门槛"
                          : health.evaluated_observations >= health.required_observations
                            ? "量化健康门禁阻断"
                            : health.healthy
                              ? `继续收集样本 ${health.evaluated_observations}/${health.required_observations}`
                              : "等待有效样本"
                  }</small>
                </div>
              )}
              <footer>
                <span>{skill.source_run_ids.length} source runs</span>
                {(["draft", "security_review", "offline_pass"]).includes(skill.status) && (
                  <button className="text-button" onClick={() => evaluate(skill)} disabled={busy === skill.skill_id}>
                    {busy === skill.skill_id ? <LoaderCircle className="spin" size={14} /> : <ShieldAlert size={14} />}
                    运行系统评测
                  </button>
                )}
                {skill.status === "shadow" && (
                  <button className="text-button" onClick={() => promote(skill, "canary")} disabled={busy === skill.skill_id || !health?.promotion_ready}>
                    {busy === skill.skill_id ? <LoaderCircle className="spin" size={14} /> : <Rocket size={14} />}
                    批准小流量
                  </button>
                )}
                {skill.status === "canary" && (
                  <button className="text-button" onClick={() => promote(skill, "active")} disabled={busy === skill.skill_id || !health?.promotion_ready}>
                    {busy === skill.skill_id ? <LoaderCircle className="spin" size={14} /> : <CheckCircle2 size={14} />}
                    批准启用
                  </button>
                )}
              </footer>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function learningChangeTitle(change: LearningChange) {
  const name = String(change.structured_diff.name ?? change.structured_diff.key ?? "").trim();
  if (change.target_type.includes("skill")) return name ? `系统建议复用“${name}”工作流` : "系统建议复用一个工作流";
  if (change.target_type.includes("memory")) return name ? `系统记录了“${name}”这条长期信息` : "系统记录了一条长期信息";
  return name ? `系统提出“${name}”的知识改进` : "系统提出了一项知识改进";
}

export function LearningView({
  changes,
  memories,
  skillEvolution,
  onOpenMemory,
  onOpenSkills
}: {
  changes: LearningChange[];
  memories: MemoryRecord[];
  skillEvolution: SkillEvolutionSnapshot[];
  onOpenMemory: () => void;
  onOpenSkills: () => void;
}) {
  const [tab, setTab] = useState<"memory" | "learned" | "review">("memory");
  const visibleMemories = memories.filter((memory) => !memory.revoked_at);
  const reviewSkills = skillEvolution.filter((snapshot) =>
    ["draft", "security_review", "offline_pass", "shadow", "canary"].includes(snapshot.skill.status)
  );
  const reviewChanges = changes.filter((change) => change.risks.length > 0);

  return (
    <section className="data-view learning-workspace">
      <header className="view-header">
        <div><span className="eyebrow">可解释、可回滚的学习</span><h1>学习</h1></div>
        <span className="view-count">{visibleMemories.length} 条记忆 · {changes.length} 项改进</span>
      </header>
      <div className="learning-tabs" role="tablist" aria-label="学习视图">
        <button role="tab" aria-selected={tab === "memory"} className={tab === "memory" ? "is-selected" : ""} onClick={() => setTab("memory")}>我的记忆</button>
        <button role="tab" aria-selected={tab === "learned"} className={tab === "learned" ? "is-selected" : ""} onClick={() => setTab("learned")}>系统学到</button>
        <button role="tab" aria-selected={tab === "review"} className={tab === "review" ? "is-selected" : ""} onClick={() => setTab("review")}>待我确认</button>
      </div>
      {tab === "memory" && (
        visibleMemories.length === 0 ? <EmptyState icon={Database} label="还没有保存的长期记忆" /> : (
          <div className="learning-list">
            {visibleMemories.slice(0, 12).map((memory) => (
              <article className="learning-item" key={memory.memory_id}>
                <div><strong>{memory.summary}</strong><span>来自 {memory.provenance.length} 个来源 · 更新于 {formatDate(memory.updated_at)}</span></div>
                <span className="learning-kind">我的记忆</span>
              </article>
            ))}
          </div>
        )
      )}
      {tab === "learned" && (
        changes.length === 0 ? <EmptyState icon={GitCommitHorizontal} label="系统还没有记录可展示的改进" /> : (
          <div className="learning-list">
            {changes.map((change) => (
              <article className="learning-item" key={change.change_set_id}>
                <div>
                  <strong>{learningChangeTitle(change)}</strong>
                  <span>{change.expected_benefits[0] ?? "系统会先通过评测和反馈门禁，再应用这项改进。"}</span>
                  <small>{change.source_run_ids.length} 次真实使用反馈 · {formatDate(change.created_at)}</small>
                </div>
                <span className="learning-kind is-system">系统学到</span>
              </article>
            ))}
          </div>
        )
      )}
      {tab === "review" && (
        reviewSkills.length === 0 && reviewChanges.length === 0 ? <EmptyState icon={ShieldAlert} label="没有需要你确认的高影响建议" /> : (
          <div className="learning-list">
            {reviewSkills.map((snapshot) => (
              <article className="learning-item" key={snapshot.skill.skill_id}>
                <div><strong>建议评估“{snapshot.skill.name}”工作流</strong><span>当前处于 {snapshot.skill.status}，尚未自动变更在线能力。</span></div>
                <button className="text-button" onClick={onOpenSkills}>查看治理详情</button>
              </article>
            ))}
            {reviewChanges.map((change) => (
              <article className="learning-item" key={change.change_set_id}>
                <div><strong>{learningChangeTitle(change)}</strong><span>{change.risks[0] || "需要先确认影响范围。"}</span></div>
                <button className="text-button" onClick={onOpenSkills}>查看治理详情</button>
              </article>
            ))}
          </div>
        )
      )}
      <footer className="learning-footer">
        <button className="text-button" onClick={onOpenMemory}>管理我的记忆</button>
        <button className="text-button" onClick={onOpenSkills}>打开技能治理</button>
      </footer>
    </section>
  );
}

type SystemMapQueryKind = "dependencies" | "impact" | "ownership" | "incidents" | "decisions" | "compare";

const systemMapQueries: Array<{ kind: SystemMapQueryKind; label: string; fallbackEntity: string }> = [
  { kind: "dependencies", label: "服务依赖", fallbackEntity: "Atlas" },
  { kind: "impact", label: "变更影响", fallbackEntity: "Atlas" },
  { kind: "ownership", label: "负责人和团队", fallbackEntity: "Atlas" },
  { kind: "incidents", label: "事故关联", fallbackEntity: "Sentinel" },
  { kind: "decisions", label: "决策演化", fallbackEntity: "ADR" },
  { kind: "compare", label: "两个实体比较", fallbackEntity: "Atlas" }
];

function mapEntities(result?: GraphResult) {
  const nodes = new Map<string, GraphNode>();
  const relationships = new Map<string, GraphRelationship>();
  result?.paths.forEach((path) => {
    path.nodes.forEach((node) => nodes.set(node.node_id, node));
    path.relationships.forEach((relationship) => relationships.set(relationship.relationship_id, relationship));
  });
  return {
    nodes: Array.from(nodes.values()).slice(0, 18),
    relationships: Array.from(relationships.values()).slice(0, 30)
  };
}

function SystemMapCanvas({
  result,
  selectedNodeId,
  onSelect
}: {
  result: GraphResult;
  selectedNodeId?: string;
  onSelect: (node: GraphNode) => void;
}) {
  const { nodes, relationships } = mapEntities(result);
  const columnCount = Math.max(2, Math.min(5, Math.ceil(Math.sqrt(nodes.length))));
  const positions = new Map(nodes.map((node, index) => {
    const column = index % columnCount;
    const row = Math.floor(index / columnCount);
    return [node.node_id, { x: 96 + column * 156, y: 78 + row * 112 }];
  }));

  return (
    <div className="system-map-canvas" aria-label="系统关系图">
      <svg viewBox="0 0 820 520" role="img" aria-label={`当前展示 ${nodes.length} 个实体和 ${relationships.length} 条关系`}>
        <defs>
          <marker id="system-map-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#9aa8a1" />
          </marker>
        </defs>
        {relationships.map((relationship) => {
          const source = positions.get(relationship.source_node_id);
          const target = positions.get(relationship.target_node_id);
          if (!source || !target) return null;
          return (
            <g key={relationship.relationship_id}>
              <line x1={source.x} y1={source.y} x2={target.x} y2={target.y} markerEnd="url(#system-map-arrow)" />
              <text x={(source.x + target.x) / 2} y={(source.y + target.y) / 2 - 7}>{relationship.relation_type}</text>
            </g>
          );
        })}
        {nodes.map((node) => {
          const position = positions.get(node.node_id);
          if (!position) return null;
          return (
            <g
              className={`system-map-node ${selectedNodeId === node.node_id ? "is-selected" : ""}`}
              key={node.node_id}
              transform={`translate(${position.x - 58} ${position.y - 29})`}
              onClick={() => onSelect(node)}
              tabIndex={0}
              role="button"
              aria-label={`查看 ${node.name}`}
              onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSelect(node); }}
            >
              <rect width="116" height="58" rx="5" />
              <text className="system-map-node-type" x="10" y="19">{node.label}</text>
              <text className="system-map-node-name" x="10" y="39">{node.name.slice(0, 18)}</text>
              <title>{node.name}</title>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

export function GraphView({
  onChanged,
  canReview = false
}: {
  onChanged: () => Promise<void> | void;
  canReview?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [template, setTemplate] = useState("paths");
  const [result, setResult] = useState<GraphResult>();
  const [mode, setMode] = useState<"explore" | "review">("explore");
  const [entityType, setEntityType] = useState("all");
  const [selectedNodeId, setSelectedNodeId] = useState<string>();
  const [candidateKind, setCandidateKind] = useState<"relations" | "entities" | "resolutions">("relations");
  const [statusFilter, setStatusFilter] = useState<"all" | GraphCandidateStatus>("pending");
  const [candidates, setCandidates] = useState<GraphCandidateCollection>({ entities: [], relations: [], resolutions: [] });
  const [searchLoading, setSearchLoading] = useState(false);
  const [candidateLoading, setCandidateLoading] = useState(false);
  const [busy, setBusy] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [error, setError] = useState<string>();

  const loadCandidates = useCallback(async () => {
    if (!canReview) return;
    setCandidateLoading(true);
    try {
      setCandidates(await api.graphCandidates());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法加载图谱候选。");
    } finally {
      setCandidateLoading(false);
    }
  }, [canReview]);

  useEffect(() => {
    if (canReview) void loadCandidates();
  }, [canReview, loadCandidates]);

  async function search() {
    if (!query.trim()) return;
    setSearchLoading(true);
    setError(undefined);
    try {
      const next = await api.graphSearch([query.trim()], template);
      setResult(next);
      setSelectedNodeId(next.paths[0]?.nodes[0]?.node_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法查询系统地图。");
    } finally {
      setSearchLoading(false);
    }
  }

  async function runFixedQuery(definition: typeof systemMapQueries[number]) {
    const rawInput = query.trim() || definition.fallbackEntity;
    const entities = definition.kind === "compare"
      ? rawInput.split(/[，,]/).map((item) => item.trim()).filter(Boolean).slice(0, 2)
      : [rawInput];
    if (entities.length === 0) return;
    setQuery(rawInput);
    setSearchLoading(true);
    setError(undefined);
    try {
      const next = await api.systemMapQuery(definition.kind, entities);
      setResult(next);
      setSelectedNodeId(next.paths[0]?.nodes[0]?.node_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法查询系统地图");
    } finally {
      setSearchLoading(false);
    }
  }

  async function review(
    kind: "entity" | "relation" | "resolution",
    candidateId: string,
    targetStatus: GraphCandidateStatus
  ) {
    const operationId = `${kind}:${candidateId}`;
    setBusy(operationId);
    setError(undefined);
    setNotice(undefined);
    try {
      if (kind === "entity") {
        await api.reviewGraphEntity(candidateId, targetStatus);
      } else if (kind === "relation") {
        await api.reviewGraphRelation(candidateId, targetStatus);
      } else {
        await api.reviewEntityResolution(candidateId, targetStatus);
      }
      setNotice(targetStatus === "approved" ? "候选已批准并进入证据图谱" : "候选已拒绝并从图检索中隔离");
      await loadCandidates();
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "候选审核没有完成。");
    } finally {
      setBusy(undefined);
    }
  }

  const visibleRelations = candidates.relations.filter(
    (item) => statusFilter === "all" || item.status === statusFilter
  );
  const visibleEntities = candidates.entities.filter(
    (item) => statusFilter === "all" || item.status === statusFilter
  );
  const visibleResolutions = candidates.resolutions.filter(
    (item) => statusFilter === "all" || item.status === statusFilter
  );
  const candidateCounts = [...candidates.entities, ...candidates.relations, ...candidates.resolutions].reduce(
    (counts, item) => ({ ...counts, [item.status]: counts[item.status] + 1 }),
    { pending: 0, approved: 0, rejected: 0, archived: 0 } as Record<GraphCandidateStatus, number>
  );
  const displayedResult: GraphResult | undefined = result
    ? {
        ...result,
        paths: entityType === "all"
          ? result.paths
          : result.paths.filter((path) => path.nodes.some((node) => node.label.toLocaleLowerCase() === entityType))
      }
    : undefined;
  const mapData = mapEntities(displayedResult);
  const selectedNode = mapData.nodes.find((node) => node.node_id === selectedNodeId) ?? mapData.nodes[0];
  const selectedRelationships = selectedNode
    ? mapData.relationships.filter((relationship) =>
        relationship.source_node_id === selectedNode.node_id || relationship.target_node_id === selectedNode.node_id
      )
    : [];

  return (
    <section className="data-view graph-view">
      <header className="view-header">
        <div><span className="eyebrow">服务、团队、事故与决策</span><h1>系统地图</h1></div>
        <span className="view-count">{result ? `${mapData.nodes.length} 个实体 · ${mapData.relationships.length} 条关系` : "使用固定查询或实体搜索"}</span>
      </header>
      <div className="system-map-fixed-queries" aria-label="固定图谱查询">
        {systemMapQueries.map((definition) => (
          <button
            key={definition.kind}
            className="text-button"
            disabled={searchLoading}
            onClick={() => void runFixedQuery(definition)}
          >
            <Network size={14} />
            {definition.label}
          </button>
        ))}
      </div>
      {error && <div className="error-banner">{error}</div>}
      {notice && <div className="notice-banner">{notice}</div>}

      {mode === "explore" && (
        <>
          <div className="graph-controls">
            <div className="search-input"><Search size={17} /><input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && search()} placeholder="输入服务、团队、事故或 ADR；比较时用逗号分隔两个实体" /></div>
            <label className="graph-type-filter">
              <span>类型</span>
              <select value={entityType} onChange={(event) => setEntityType(event.target.value)}>
                <option value="all">全部</option>
                <option value="service">服务</option>
                <option value="api">API</option>
                <option value="team">团队</option>
                <option value="decision">决策</option>
                <option value="incident">事故</option>
                <option value="runbook">Runbook</option>
                <option value="technology">技术</option>
              </select>
            </label>
            <div className="segmented-control">
              {[
                ["neighbors", "邻居"],
                ["paths", "路径"],
                ["conflicts", "冲突"]
              ].map(([value, label]) => (
                <button
                  key={value}
                  className={template === value ? "is-selected" : ""}
                  onClick={() => setTemplate(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <button className="primary-button" onClick={search} disabled={searchLoading}>{searchLoading ? <LoaderCircle size={16} className="spin" /> : <Network size={16} />}查询</button>
            {canReview && (
              <button className="icon-button" title="候选审核" onClick={() => setMode("review")}>
                <ListChecks size={16} />
              </button>
            )}
          </div>
          {!displayedResult ? (
            <div className="system-map-empty">
              <Network size={23} />
              <strong>从研发对象开始探索</strong>
              <span>固定查询只会调用受控的图谱模板，不会向服务端发送 Cypher。</span>
            </div>
          ) : displayedResult.paths.length === 0 ? (
            <div className="system-map-empty">
              <Network size={23} />
              <strong>没有符合当前筛选的关系</strong>
              <span>可以切换实体类型、清除筛选，或检查资料是否已经完成图谱抽取。</span>
            </div>
          ) : (
            <div className="system-map-layout">
              <div className="system-map-main">
                <div className="graph-summary"><strong>{displayedResult.paths.length}</strong><span>条路径</span><strong>{displayedResult.evidence.length}</strong><span>项支持来源</span></div>
                <SystemMapCanvas
                  result={displayedResult}
                  selectedNodeId={selectedNode?.node_id}
                  onSelect={(node) => setSelectedNodeId(node.node_id)}
                />
              </div>
              <aside className="system-map-detail">
                {selectedNode ? (
                  <>
                    <span className="eyebrow">实体详情</span>
                    <h2>{selectedNode.name}</h2>
                    <span className="system-map-node-label">{selectedNode.label}</span>
                    <dl>
                      {Object.entries(selectedNode.properties).slice(0, 5).map(([key, value]) => (
                        <div key={key}><dt>{key}</dt><dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd></div>
                      ))}
                    </dl>
                    <h3>关联关系</h3>
                    {selectedRelationships.length === 0 ? <span className="muted-text">当前结果没有可展示的相邻关系。</span> : (
                      <div className="system-map-relations">
                        {selectedRelationships.map((relationship) => <span key={relationship.relationship_id}>{relationship.relation_type}</span>)}
                      </div>
                    )}
                    <h3>支持来源</h3>
                    <span className="muted-text">本次地图结果有 {displayedResult.evidence.length} 项可追溯来源。</span>
                  </>
                ) : <span className="muted-text">选择一个实体查看详情。</span>}
              </aside>
            </div>
          )}
        </>
      )}

      {canReview && mode === "review" && (
        <div className="candidate-review">
          <div className="graph-mode-bar"><button className="text-button" onClick={() => setMode("explore")}><Search size={14} />返回系统地图</button></div>
          <div className="candidate-count-strip">
            <div><span>待审核</span><strong>{candidateCounts.pending}</strong></div>
            <div><span>已批准</span><strong>{candidateCounts.approved}</strong></div>
            <div><span>已拒绝</span><strong>{candidateCounts.rejected}</strong></div>
            <div><span>已归档</span><strong>{candidateCounts.archived}</strong></div>
          </div>
          <div className="candidate-toolbar">
            <div className="segmented-control" aria-label="候选类型">
              <button className={candidateKind === "relations" ? "is-selected" : ""} onClick={() => setCandidateKind("relations")}>关系 {candidates.relations.length}</button>
              <button className={candidateKind === "entities" ? "is-selected" : ""} onClick={() => setCandidateKind("entities")}>实体 {candidates.entities.length}</button>
              <button className={candidateKind === "resolutions" ? "is-selected" : ""} onClick={() => setCandidateKind("resolutions")}><GitMerge size={12} />归并 {candidates.resolutions.length}</button>
            </div>
            <div className="candidate-filter">
              <label htmlFor="candidate-status">状态</label>
              <select id="candidate-status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as "all" | GraphCandidateStatus)}>
                <option value="pending">待审核</option>
                <option value="approved">已批准</option>
                <option value="rejected">已拒绝</option>
                <option value="archived">已归档</option>
                <option value="all">全部</option>
              </select>
              <button className="icon-button" title="刷新候选" onClick={() => void loadCandidates()} disabled={candidateLoading}>
                <RefreshCw size={15} className={candidateLoading ? "spin" : ""} />
              </button>
            </div>
          </div>

          {candidateKind === "relations" && (
            visibleRelations.length === 0 ? <EmptyState icon={ListChecks} label="当前筛选下没有关系候选" /> : (
              <div className="candidate-table-wrap">
                <div className="candidate-table relation-candidates">
                  <div className="candidate-head"><span>来源实体</span><span>关系</span><span>目标实体</span><span>置信度</span><span>状态</span><span /></div>
                  {visibleRelations.map((item) => {
                    const operationId = `relation:${item.candidate_id}`;
                    return (
                      <div className="candidate-row" key={item.candidate_id}>
                        <span className="candidate-name"><strong>{item.source_name}</strong><small>{item.source_chunk_ids.length} evidence</small></span>
                        <code>{item.relation_type}</code>
                        <span className="candidate-name"><strong>{item.target_name}</strong><small>{item.extractor_revision}</small></span>
                        <span className="confidence-meter"><i style={{ width: `${Math.round(item.confidence * 100)}%` }} /><small>{Math.round(item.confidence * 100)}%</small></span>
                        <span className={`candidate-status status-${item.status}`}>{item.status}</span>
                        <span className="review-actions">
                          {item.status === "pending" && <button title="批准关系" aria-label={`批准 ${item.source_name} ${item.relation_type} ${item.target_name}`} onClick={() => void review("relation", item.candidate_id, "approved")} disabled={busy === operationId}><Check size={15} /></button>}
                          {(item.status === "pending" || item.status === "approved") && <button className="reject" title="拒绝关系" aria-label={`拒绝 ${item.source_name} ${item.relation_type} ${item.target_name}`} onClick={() => void review("relation", item.candidate_id, "rejected")} disabled={busy === operationId}><X size={15} /></button>}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )
          )}

          {candidateKind === "entities" && (
            visibleEntities.length === 0 ? <EmptyState icon={Network} label="当前筛选下没有实体候选" /> : (
              <div className="candidate-table-wrap">
                <div className="candidate-table entity-candidates">
                  <div className="candidate-head"><span>规范名称</span><span>类型</span><span>来源</span><span>置信度</span><span>状态</span><span /></div>
                  {visibleEntities.map((item) => {
                    const operationId = `entity:${item.candidate_id}`;
                    return (
                      <div className="candidate-row" key={item.candidate_id}>
                        <span className="candidate-name"><strong>{item.canonical_name}</strong><small>{item.aliases.join(" · ") || item.candidate_id.slice(0, 8)}</small></span>
                        <code>{item.entity_type}</code>
                        <span>{item.source_chunk_ids.length} chunks</span>
                        <span className="confidence-meter"><i style={{ width: `${Math.round(item.confidence * 100)}%` }} /><small>{Math.round(item.confidence * 100)}%</small></span>
                        <span className={`candidate-status status-${item.status}`}>{item.status}</span>
                        <span className="review-actions">
                          {item.status === "pending" && <button title="批准实体" aria-label={`批准实体 ${item.canonical_name}`} onClick={() => void review("entity", item.candidate_id, "approved")} disabled={busy === operationId}><Check size={15} /></button>}
                          {(item.status === "pending" || item.status === "approved") && <button className="reject" title="拒绝实体" aria-label={`拒绝实体 ${item.canonical_name}`} onClick={() => void review("entity", item.candidate_id, "rejected")} disabled={busy === operationId}><X size={15} /></button>}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )
          )}

          {candidateKind === "resolutions" && (
            visibleResolutions.length === 0 ? <EmptyState icon={GitMerge} label="当前筛选下没有实体归并建议" /> : (
              <div className="candidate-table-wrap">
                <div className="candidate-table resolution-candidates">
                  <div className="candidate-head"><span>实体 A</span><span>匹配</span><span>实体 B</span><span>置信度</span><span>状态</span><span /></div>
                  {visibleResolutions.map((item) => {
                    const operationId = `resolution:${item.candidate_id}`;
                    return (
                      <div className="candidate-row" key={item.candidate_id}>
                        <span className="candidate-name"><strong>{item.left_name}</strong><small>{item.left_document_id.slice(0, 8)} · {item.entity_type}</small></span>
                        <code>{item.match_strategy}</code>
                        <span className="candidate-name"><strong>{item.right_name}</strong><small>{item.right_document_id.slice(0, 8)} · {item.source_chunk_ids.length} evidence</small></span>
                        <span className="confidence-meter"><i style={{ width: `${Math.round(item.confidence * 100)}%` }} /><small>{Math.round(item.confidence * 100)}%</small></span>
                        <span className={`candidate-status status-${item.status}`}>{item.status}</span>
                        <span className="review-actions">
                          {item.status === "pending" && <button title="批准实体归并" aria-label={`批准归并 ${item.left_name} 和 ${item.right_name}`} onClick={() => void review("resolution", item.candidate_id, "approved")} disabled={busy === operationId}><Check size={15} /></button>}
                          {(item.status === "pending" || item.status === "approved") && <button className="reject" title="拒绝实体归并" aria-label={`拒绝归并 ${item.left_name} 和 ${item.right_name}`} onClick={() => void review("resolution", item.candidate_id, "rejected")} disabled={busy === operationId}><X size={15} /></button>}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )
          )}
        </div>
      )}
    </section>
  );
}

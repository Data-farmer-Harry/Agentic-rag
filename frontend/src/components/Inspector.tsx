import { useEffect, useState } from "react";
import {
  CheckCircle2,
  Database,
  ExternalLink,
  Hammer,
  ShieldCheck,
  Sparkles,
  X,
  XCircle
} from "lucide-react";
import { api } from "../api";
import { describeTool, formatDuration } from "../runActivity";
import type { Evidence, Overview, RunTrajectory, ToolEvent } from "../types";

interface InspectorProps {
  overview?: Overview;
  statusLabel: string;
  toolEvents: ToolEvent[];
  evidence: Evidence[];
  selectedEvidence?: Evidence;
  selectedRun?: RunTrajectory;
  running: boolean;
  mobileOpen: boolean;
  onClose: () => void;
}

function sourceTitle(evidence: Evidence) {
  return evidence.title || evidence.provenance.source_id;
}

function visualBoundingBox(evidence: Evidence): number[] | undefined {
  const value = evidence.provenance.locator?.bounding_box;
  if (
    Array.isArray(value) &&
    value.length === 4 &&
    value.every((item) => typeof item === "number" && item >= 0 && item <= 1)
  ) {
    return value as number[];
  }
  return undefined;
}

function AuthenticatedImage({ documentId, alt }: { documentId: string; alt: string }) {
  const [source, setSource] = useState<string>();

  useEffect(() => {
    let active = true;
    let objectUrl: string | undefined;
    void api.documentContent(documentId).then((blob) => {
      if (!active) return;
      objectUrl = window.URL.createObjectURL(blob);
      setSource(objectUrl);
    }).catch((error) => console.error("Unable to load evidence image", error));
    return () => {
      active = false;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [documentId]);

  return source ? <img src={source} alt={alt} /> : <div className="visual-preview-loading" />;
}

interface RetrievalTraceView {
  intent: string;
  planner: string;
  stopReason: string;
  roundCount: number;
  queries: string[];
  gapReasons: string[];
  fallbackError?: string;
}

function retrievalTrace(tool: ToolEvent): RetrievalTraceView | undefined {
  const detail = tool.detail;
  if (!detail || typeof detail.controller !== "string") return undefined;
  const plan = detail.plan && typeof detail.plan === "object"
    ? detail.plan as Record<string, unknown>
    : {};
  const rounds = Array.isArray(detail.rounds) ? detail.rounds : [];
  const queries = rounds.flatMap((round) => {
    if (!round || typeof round !== "object") return [];
    const value = (round as Record<string, unknown>).queries;
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  });
  const gaps = Array.isArray(detail.gap_assessments) ? detail.gap_assessments : [];
  const lastGap = gaps.length && gaps[gaps.length - 1] && typeof gaps[gaps.length - 1] === "object"
    ? gaps[gaps.length - 1] as Record<string, unknown>
    : {};
  const reasons = Array.isArray(lastGap.reasons)
    ? lastGap.reasons.filter((item): item is string => typeof item === "string")
    : [];
  return {
    intent: typeof plan.intent === "string" ? plan.intent : "lookup",
    planner: typeof detail.planner_revision === "string" ? detail.planner_revision : "unknown",
    stopReason: typeof detail.stop_reason === "string" ? detail.stop_reason : "unknown",
    roundCount: rounds.length,
    queries,
    gapReasons: reasons,
    fallbackError: typeof detail.planner_fallback_error === "string"
      ? detail.planner_fallback_error
      : undefined
  };
}

export function Inspector({
  overview,
  statusLabel,
  toolEvents,
  evidence,
  selectedEvidence,
  selectedRun,
  running,
  mobileOpen,
  onClose
}: InspectorProps) {
  if (selectedEvidence) {
    const documentId = selectedEvidence.metadata.document_id;
    const isVisual =
      selectedEvidence.metadata.modality === "image" && typeof documentId === "string";
    const boundingBox = visualBoundingBox(selectedEvidence);
    return (
      <aside className={`inspector ${mobileOpen ? "is-mobile-open" : ""}`}>
        <div className="inspector-header">
          <div>
            <span className="eyebrow">Evidence</span>
            <h2>{sourceTitle(selectedEvidence)}</h2>
          </div>
          <button className="inspector-close" onClick={onClose} title="关闭详情"><X size={17} /></button>
        </div>
        <div className="inspector-scroll">
          <div className="evidence-metadata">
            <span>{selectedEvidence.provenance.source_type}</span>
            <span>{selectedEvidence.provenance.trust}</span>
            <span>score {selectedEvidence.score.toFixed(3)}</span>
          </div>
          {isVisual && (
            <div className="visual-evidence-preview">
              <AuthenticatedImage documentId={documentId} alt={sourceTitle(selectedEvidence)} />
              {boundingBox && (
                <span
                  className="visual-region-box"
                  style={{
                    left: `${boundingBox[0] * 100}%`,
                    top: `${boundingBox[1] * 100}%`,
                    width: `${(boundingBox[2] - boundingBox[0]) * 100}%`,
                    height: `${(boundingBox[3] - boundingBox[1]) * 100}%`
                  }}
                />
              )}
            </div>
          )}
          <div className="evidence-fulltext">{selectedEvidence.text}</div>
          <div className="provenance-block">
            <strong>来源标识</strong>
            <code>{selectedEvidence.provenance.source_id}</code>
            {selectedEvidence.provenance.content_hash && (
              <>
                <strong>内容哈希</strong>
                <code>{selectedEvidence.provenance.content_hash.slice(0, 24)}…</code>
              </>
            )}
          </div>
        </div>
      </aside>
    );
  }

  if (selectedRun) {
    const retrievalTraces = selectedRun.tool_events
      .map((tool) => ({ tool, trace: retrievalTrace(tool) }))
      .filter((item): item is { tool: ToolEvent; trace: RetrievalTraceView } => Boolean(item.trace));
    return (
      <aside className={`inspector ${mobileOpen ? "is-mobile-open" : ""}`}>
        <div className="inspector-header">
          <div>
            <span className="eyebrow">Run snapshot</span>
            <h2>{selectedRun.context.run_id.slice(0, 8)}</h2>
          </div>
          <button className="inspector-close" onClick={onClose} title="关闭详情"><X size={17} /></button>
        </div>
        <div className="inspector-scroll">
          <dl className="detail-list">
            <div><dt>状态</dt><dd>{selectedRun.status}</dd></div>
            <div><dt>领域包</dt><dd>{selectedRun.context.domain_pack}</dd></div>
            <div><dt>模型</dt><dd>{selectedRun.context.model}</dd></div>
            <div><dt>工具调用</dt><dd>{selectedRun.tool_events.length}</dd></div>
            <div><dt>回答模式</dt><dd>{selectedRun.answer?.response_mode ?? "grounded"}</dd></div>
            {(!selectedRun.answer || selectedRun.answer.response_mode === "grounded") && (
              <div><dt>置信级别</dt><dd>{selectedRun.answer?.confidence ?? "-"}</dd></div>
            )}
          </dl>
          <h3 className="inspector-section-title">任务</h3>
          <p className="run-prompt-detail">{selectedRun.user_input}</p>
          {retrievalTraces.length > 0 && (
            <>
              <h3 className="inspector-section-title">Agentic Retrieval</h3>
              <div className="retrieval-trace-list">
                {retrievalTraces.map(({ tool, trace }, index) => (
                  <article key={`${tool.input_hash}-${index}`} className="retrieval-trace-item">
                    <div className="retrieval-trace-summary">
                      <strong>{trace.intent}</strong>
                      <span>{trace.roundCount} 轮</span>
                      <span>{trace.stopReason}</span>
                    </div>
                    <small>{trace.planner}</small>
                    {trace.fallbackError && <small>fallback: {trace.fallbackError}</small>}
                    <ol>
                      {trace.queries.map((query, queryIndex) => (
                        <li key={`${query}-${queryIndex}`}>{query}</li>
                      ))}
                    </ol>
                    {trace.gapReasons.length > 0 && (
                      <small>剩余缺口：{trace.gapReasons.join("、")}</small>
                    )}
                  </article>
                ))}
              </div>
            </>
          )}
          <h3 className="inspector-section-title">固定技能版本</h3>
          <div className="key-value-list">
            {Object.entries(selectedRun.context.skill_versions).length ? (
              Object.entries(selectedRun.context.skill_versions).map(([name, version]) => (
                <div key={name}><span>{name}</span><code>{version}</code></div>
              ))
            ) : (
              <span className="muted-text">本次运行未激活技能</span>
            )}
          </div>
        </div>
      </aside>
    );
  }

  return (
    <aside className={`inspector ${mobileOpen ? "is-mobile-open" : ""}`}>
      <div className="inspector-header">
        <div>
          <span className="eyebrow">Live trace</span>
          <h2>{running ? statusLabel : "运行上下文"}</h2>
        </div>
        <div className="inspector-header-actions">
          {running ? <Hammer size={19} /> : <ShieldCheck size={19} />}
          <button className="inspector-close mobile-only" onClick={onClose} title="关闭详情"><X size={17} /></button>
        </div>
      </div>
      <div className="inspector-scroll">
        <div className="trace-list">
          <div className={`trace-row ${running ? "is-current" : "is-complete"}`}>
            <span className="trace-icon"><CheckCircle2 size={15} /></span>
            <div><strong>请求已接收</strong><span>范围与领域包已锁定</span></div>
          </div>
          {toolEvents.map((tool, index) => {
            const description = describeTool(tool);
            return (
              <div className="trace-row is-complete" key={`${tool.input_hash}-${tool.created_at}-${index}`}>
                <span className="trace-icon">
                  {tool.success ? <CheckCircle2 size={15} /> : <XCircle size={15} />}
                </span>
                <div>
                  <strong>{description.title}</strong>
                  <span>{tool.success ? description.detail : "工具执行失败"}</span>
                </div>
                <time>{formatDuration(tool.duration_ms)}</time>
              </div>
            );
          })}
          {running && (
            <div className="trace-row is-current">
              <span className="trace-pulse" />
              <div><strong>{statusLabel}</strong><span>事件流保持连接</span></div>
            </div>
          )}
        </div>

        {evidence.length > 0 && (
          <>
            <h3 className="inspector-section-title">本轮证据</h3>
            <div className="source-list">
              {evidence.map((item, index) => (
                <div key={item.evidence_id} className="source-row">
                  <span>{index + 1}</span>
                  <div><strong>{sourceTitle(item)}</strong><small>{item.provenance.trust}</small></div>
                  <ExternalLink size={14} />
                </div>
              ))}
            </div>
          </>
        )}

        {!running && toolEvents.length === 0 && (
          <div className="runtime-summary">
            <div><Database size={16} /><span>Retrieval</span><strong>{overview?.retrieval_backend ?? "local"}</strong></div>
            <div><Database size={16} /><span>Memory</span><strong>{overview?.counts.memories ?? 0}</strong></div>
            <div><Sparkles size={16} /><span>Skills</span><strong>{overview?.counts.skills ?? 0}</strong></div>
            <div><ShieldCheck size={16} /><span>Capabilities</span><strong>{overview?.capabilities.length ?? 0}</strong></div>
          </div>
        )}
      </div>
    </aside>
  );
}

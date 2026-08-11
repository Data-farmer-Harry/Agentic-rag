import {
  Ban,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  GitFork,
  LoaderCircle,
  XCircle
} from "lucide-react";
import { describeTool, formatDuration } from "../runActivity";
import type { Evidence, RetrievalRouteDecision, ToolEvent } from "../types";

interface RunActivityTimelineProps {
  status?: string;
  streaming?: boolean;
  cancelled?: boolean;
  error?: string;
  elapsedMs?: number;
  toolEvents: ToolEvent[];
  route?: RetrievalRouteDecision;
  evidence?: Evidence[];
  graphPathCount?: number;
}

const ROUTE_PRESENTATION = {
  conversation: {
    title: "直接对话",
    detail: "Adaptive-RAG 判断当前请求无需检索"
  },
  tool_action: {
    title: "直接执行",
    detail: "无需知识检索，仅调用完成任务所需的工具"
  },
  passage_lookup: {
    title: "文档片段检索",
    detail: "优先定位与问题最相关的原文片段"
  },
  relationship: {
    title: "实体关系检索",
    detail: "优先沿知识图谱查找实体、关系和关联证据"
  },
  global_summary: {
    title: "跨文档全局总结",
    detail: "从多个来源聚合主题、趋势与共性信息"
  }
} as const;

function currentStepDetail(status?: string, elapsedMs?: number) {
  if (status?.includes("恢复")) {
    return "正在从已确认进度继续，不会重复显示已经收到的事件";
  }
  if ((elapsedMs ?? 0) >= 30_000) {
    return "任务仍在后台运行；刷新页面后可以继续查看，也可以随时停止";
  }
  if ((elapsedMs ?? 0) >= 10_000) {
    return "复杂问题可能需要多轮检索，可以继续等待或停止后调整问题";
  }
  return "连接正常，进度会在步骤完成后自动更新";
}

export function RunActivityTimeline({
  status,
  streaming,
  cancelled,
  error,
  elapsedMs,
  toolEvents,
  route,
  evidence = [],
  graphPathCount = 0
}: RunActivityTimelineProps) {
  const completed = !streaming && !error && !cancelled;
  const summary = streaming
    ? status || "正在执行任务"
    : cancelled
      ? "任务已停止"
      : error
        ? "任务未完成"
        : `执行完成${toolEvents.length ? ` · ${toolEvents.length} 个工具` : ""}`;
  const distinctSources = new Set(
    evidence.map((item) => item.provenance.source_id).filter(Boolean)
  ).size;
  const routeView = route ? ROUTE_PRESENTATION[route.route] : undefined;

  return (
    <details
      className={`run-activity ${streaming ? "is-running" : ""}`}
      open={streaming || undefined}
      aria-live={streaming ? "polite" : "off"}
    >
      <summary>
        <span className="run-activity-state">
          {streaming ? (
            <LoaderCircle size={14} className="spin" />
          ) : cancelled ? (
            <Ban size={14} />
          ) : error ? (
            <XCircle size={14} />
          ) : (
            <CheckCircle2 size={14} />
          )}
        </span>
        <span>{summary}</span>
        {elapsedMs !== undefined && <time>{formatDuration(elapsedMs)}</time>}
        <ChevronDown size={14} className="run-activity-chevron" />
      </summary>
      <div className="run-activity-steps">
        <div className="run-activity-step is-complete">
          <CheckCircle2 size={14} />
          <span><strong>请求已接收</strong><small>会话范围和领域上下文已锁定</small></span>
        </div>
        {route && routeView && (
          <div className="run-activity-step is-route">
            <GitFork size={14} />
            <span>
              <strong>{routeView.title}</strong>
              <small>{routeView.detail}</small>
              <span className="run-route-tags" aria-label="检索策略属性">
                {route.requires_graph && <em>知识图谱</em>}
                {route.requires_multi_source && <em>多来源</em>}
                {route.self_reflection && <em>Self-RAG</em>}
                {route.strategy === "single_step" && <em>单步检索</em>}
                <em>{route.confidence === "high" ? "高置信路由" : "标准路由"}</em>
              </span>
            </span>
          </div>
        )}
        {toolEvents.map((tool, index) => {
          const description = describeTool(tool);
          return (
            <div
              className={`run-activity-step ${tool.success ? "is-complete" : "is-error"}`}
              key={`${tool.input_hash}-${tool.created_at}-${index}`}
            >
              {tool.success ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
              <span>
                <strong>{description.title}</strong>
                <small>{tool.success ? description.detail : "这一步没有成功，任务已继续执行或结束"}</small>
              </span>
              <time>{formatDuration(tool.duration_ms)}</time>
            </div>
          );
        })}
        {streaming && (
          <div className="run-activity-step is-current">
            <CircleDot size={14} />
            <span>
              <strong>{status || "Agent 正在处理"}</strong>
              <small>{currentStepDetail(status, elapsedMs)}</small>
            </span>
          </div>
        )}
        {cancelled && (
          <div className="run-activity-step is-cancelled">
            <Ban size={14} />
            <span><strong>已停止本次任务</strong><small>已完成的步骤保留在运行记录中</small></span>
          </div>
        )}
        {error && !cancelled && (
          <div className="run-activity-step is-error">
            <XCircle size={14} />
            <span><strong>任务未完成</strong><small>{error}</small></span>
          </div>
        )}
        {completed && (
          <div className="run-activity-step is-complete">
            <CheckCircle2 size={14} />
            <span>
              <strong>回答已生成</strong>
              <small>
                {evidence.length > 0
                  ? `${evidence.length} 条证据 · ${distinctSources} 个来源${graphPathCount ? ` · ${graphPathCount} 条图谱路径` : ""}`
                  : "本轮公开输出已经完成"}
              </small>
            </span>
          </div>
        )}
      </div>
    </details>
  );
}

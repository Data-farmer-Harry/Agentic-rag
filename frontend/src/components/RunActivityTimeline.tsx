import {
  Ban,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  LoaderCircle,
  XCircle
} from "lucide-react";
import { describeTool, formatDuration } from "../runActivity";
import type { ToolEvent } from "../types";

interface RunActivityTimelineProps {
  status?: string;
  streaming?: boolean;
  cancelled?: boolean;
  error?: string;
  elapsedMs?: number;
  toolEvents: ToolEvent[];
}

export function RunActivityTimeline({
  status,
  streaming,
  cancelled,
  error,
  elapsedMs,
  toolEvents
}: RunActivityTimelineProps) {
  const completed = !streaming && !error && !cancelled;
  const summary = streaming
    ? status || "正在执行任务"
    : cancelled
      ? "任务已停止"
      : error
        ? "任务未完成"
        : `执行完成${toolEvents.length ? ` · ${toolEvents.length} 个工具` : ""}`;

  return (
    <details className={`run-activity ${streaming ? "is-running" : ""}`} open={streaming || undefined}>
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
            <span><strong>{status || "Agent 正在处理"}</strong><small>连接正常，可以随时停止</small></span>
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
            <span><strong>回答已生成</strong><small>本轮公开输出已经完成</small></span>
          </div>
        )}
      </div>
    </details>
  );
}

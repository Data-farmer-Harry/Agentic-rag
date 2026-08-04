import { useEffect, useState, type FormEvent } from "react";
import {
  CalendarClock,
  Check,
  FileText,
  ListTodo,
  LoaderCircle,
  X
} from "lucide-react";
import { api } from "../api";

export type QuickCaptureResult =
  | { kind: "task" | "schedule"; id: string; title: string; date?: string }
  | { kind: "note"; id: string; title: string; date: string };

interface QuickCaptureProps {
  onClose: () => void;
  onCreated: (result: QuickCaptureResult) => Promise<void> | void;
  initialMode?: "task" | "schedule" | "note";
  initialTitle?: string;
  initialContent?: string;
}

function localDate(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export function QuickCapture({
  onClose,
  onCreated,
  initialMode = "task",
  initialTitle = "",
  initialContent = ""
}: QuickCaptureProps) {
  const [mode, setMode] = useState<"task" | "schedule" | "note">(initialMode);
  const [title, setTitle] = useState(initialTitle);
  const [content, setContent] = useState(initialContent);
  const [priority, setPriority] = useState(3);
  const [date, setDate] = useState(localDate);
  const [time, setTime] = useState("18:00");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    setMode(initialMode);
    setTitle(initialTitle);
    setContent(initialContent);
  }, [initialContent, initialMode, initialTitle]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const safeTitle = title.trim();
    if (!safeTitle || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      if (mode === "note") {
        const note = await api.upsertPersonalNote({
          kind: "daily",
          title: safeTitle,
          content: content.trim(),
          note_date: date
        });
        await onCreated({ kind: "note", id: note.note_id, title: note.title, date });
      } else {
        const dueAt = mode === "schedule"
          ? new Date(`${date}T${time}:00`).toISOString()
          : undefined;
        const task = await api.createPersonalTask({
          title: safeTitle,
          description: content.trim(),
          priority,
          due_at: dueAt,
          tags: mode === "schedule" ? ["scheduled"] : []
        });
        await onCreated({
          kind: mode,
          id: task.task_id,
          title: task.title,
          date: mode === "schedule" ? date : undefined
        });
      }
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "记录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="quick-capture" aria-label="快速记录">
      <header>
        <div className="segmented-control" aria-label="记录类型">
          <button className={mode === "task" ? "is-selected" : ""} onClick={() => setMode("task")}>
            <ListTodo size={13} />任务
          </button>
          <button className={mode === "schedule" ? "is-selected" : ""} onClick={() => setMode("schedule")}>
            <CalendarClock size={13} />安排
          </button>
          <button className={mode === "note" ? "is-selected" : ""} onClick={() => setMode("note")}>
            <FileText size={13} />笔记
          </button>
        </div>
        <button className="icon-button" title="关闭快速记录" onClick={onClose}>
          <X size={15} />
        </button>
      </header>
      <form onSubmit={submit}>
        <input
          autoFocus
          aria-label={mode === "note" ? "笔记标题" : "任务标题"}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder={mode === "note" ? "笔记标题" : mode === "schedule" ? "安排什么" : "要完成什么"}
        />
        <textarea
          aria-label="补充内容"
          rows={2}
          value={content}
          onChange={(event) => setContent(event.target.value)}
          placeholder={mode === "note" ? "写下内容" : "补充说明（可选）"}
        />
        <div className="quick-capture-options">
          {mode !== "task" && (
            <input aria-label="日期" type="date" value={date} onChange={(event) => setDate(event.target.value)} />
          )}
          {mode === "schedule" && (
            <input aria-label="时间" type="time" value={time} onChange={(event) => setTime(event.target.value)} />
          )}
          {mode !== "note" && (
            <select aria-label="优先级" value={priority} onChange={(event) => setPriority(Number(event.target.value))}>
              <option value={1}>P1</option>
              <option value={2}>P2</option>
              <option value={3}>P3</option>
              <option value={4}>P4</option>
              <option value={5}>P5</option>
            </select>
          )}
          <button className="primary-button" disabled={!title.trim() || busy}>
            {busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
            保存
          </button>
        </div>
        {error && <div className="quick-capture-error">{error}</div>}
      </form>
    </section>
  );
}

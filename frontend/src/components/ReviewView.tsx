import { useCallback, useEffect, useMemo, useState } from "react";
import {
  BookOpenText,
  CalendarClock,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Circle,
  FileText,
  LoaderCircle,
  Plus,
  RotateCcw,
  Save,
  Sparkles
} from "lucide-react";
import { api } from "../api";
import type { DayArchive, PersonalNote, PersonalTask } from "../types";

const weekDays = ["一", "二", "三", "四", "五", "六", "日"];

function isoDate(value: Date) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function monthBounds(value: Date) {
  const start = new Date(value.getFullYear(), value.getMonth(), 1);
  const end = new Date(value.getFullYear(), value.getMonth() + 1, 0);
  return { start: isoDate(start), end: isoDate(end) };
}

function monthCells(value: Date) {
  const first = new Date(value.getFullYear(), value.getMonth(), 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const gridStart = new Date(first);
  gridStart.setDate(first.getDate() - mondayOffset);
  return Array.from({ length: 42 }, (_, index) => {
    const day = new Date(gridStart);
    day.setDate(gridStart.getDate() + index);
    return day;
  });
}

function taskDate(task: PersonalTask) {
  return task.due_at ? isoDate(new Date(task.due_at)) : undefined;
}

interface ReviewViewProps {
  initialDate?: string;
}

export function ReviewView({ initialDate }: ReviewViewProps) {
  const today = useMemo(() => new Date(), []);
  const initialDay = useMemo(
    () => initialDate ? new Date(`${initialDate}T12:00:00`) : today,
    [initialDate, today]
  );
  const [month, setMonth] = useState(new Date(initialDay.getFullYear(), initialDay.getMonth(), 1));
  const [selectedDate, setSelectedDate] = useState(isoDate(initialDay));
  const [archives, setArchives] = useState<DayArchive[]>([]);
  const [tasks, setTasks] = useState<PersonalTask[]>([]);
  const [notes, setNotes] = useState<PersonalNote[]>([]);
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [summary, setSummary] = useState("");
  const [diary, setDiary] = useState("");
  const [highlights, setHighlights] = useState("");
  const [decisions, setDecisions] = useState("");
  const [openLoops, setOpenLoops] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const selectedArchive = archives.find((item) => item.archive_date === selectedDate);
  const archiveDates = useMemo(
    () => new Set(archives.map((item) => item.archive_date)),
    [archives]
  );
  const taskCounts = useMemo(() => {
    const counts = new Map<string, number>();
    tasks.forEach((task) => {
      const date = taskDate(task);
      if (date) counts.set(date, (counts.get(date) ?? 0) + 1);
    });
    return counts;
  }, [tasks]);
  const selectedTasks = useMemo(
    () => tasks.filter((task) => taskDate(task) === selectedDate),
    [selectedDate, tasks]
  );
  const cells = useMemo(() => monthCells(month), [month]);

  const loadMonth = useCallback(async () => {
    const bounds = monthBounds(month);
    const [nextArchives, nextTasks] = await Promise.all([
      api.dayArchives(bounds.start, bounds.end),
      api.personalTasks()
    ]);
    setArchives(nextArchives);
    setTasks(nextTasks);
  }, [month]);

  const loadNotes = useCallback(async () => {
    setNotes(await api.personalNotes(undefined, selectedDate));
  }, [selectedDate]);

  useEffect(() => {
    void loadMonth().catch((cause) =>
      setError(cause instanceof Error ? cause.message : "无法载入日归档")
    );
  }, [loadMonth]);

  useEffect(() => {
    void loadNotes().catch((cause) =>
      setError(cause instanceof Error ? cause.message : "无法载入当天笔记")
    );
  }, [loadNotes]);

  useEffect(() => {
    setSummary(selectedArchive?.summary ?? "");
    setDiary(selectedArchive?.diary ?? "");
    setHighlights((selectedArchive?.highlights ?? []).join("\n"));
    setDecisions((selectedArchive?.decisions ?? []).join("\n"));
    setOpenLoops((selectedArchive?.open_loops ?? []).join("\n"));
  }, [selectedArchive]);

  async function mutate(operation: () => Promise<unknown>) {
    setBusy(true);
    setError(undefined);
    try {
      await operation();
      await Promise.all([loadMonth(), loadNotes()]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  function moveMonth(offset: number) {
    const next = new Date(month.getFullYear(), month.getMonth() + offset, 1);
    setMonth(next);
    setSelectedDate(isoDate(next));
  }

  function lines(value: string) {
    return value
      .split("\n")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function saveArchive() {
    if (!selectedArchive) return;
    void mutate(() =>
      api.updateDay(selectedDate, {
        summary,
        diary,
        highlights: lines(highlights),
        decisions: lines(decisions),
        open_loops: lines(openLoops),
        expected_version: selectedArchive.version
      })
    );
  }

  function toggleTask(task: PersonalTask) {
    void mutate(() =>
      api.updatePersonalTask(task.task_id, {
        status: task.status === "completed" ? "in_progress" : "completed",
        expected_version: task.version
      })
    );
  }

  function createDailyNote() {
    const title = noteTitle.trim();
    if (!title) return;
    void mutate(async () => {
      await api.upsertPersonalNote({
        kind: "daily",
        title,
        content: noteContent.trim(),
        note_date: selectedDate
      });
      setNoteTitle("");
      setNoteContent("");
    });
  }

  return (
    <section className="data-view personal-view review-view">
      <header className="view-header">
        <div>
          <span className="eyebrow">Daily archive</span>
          <h1>日历回顾</h1>
        </div>
        {busy && <LoaderCircle className="spin" size={17} />}
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="review-layout">
        <section className="calendar-pane">
          <header className="calendar-header">
            <button className="icon-button" title="上个月" onClick={() => moveMonth(-1)}>
              <ChevronLeft size={17} />
            </button>
            <strong>
              {month.getFullYear()} 年 {month.getMonth() + 1} 月
            </strong>
            <button className="icon-button" title="下个月" onClick={() => moveMonth(1)}>
              <ChevronRight size={17} />
            </button>
          </header>
          <div className="calendar-weekdays">
            {weekDays.map((day) => <span key={day}>{day}</span>)}
          </div>
          <div className="calendar-grid">
            {cells.map((day) => {
              const value = isoDate(day);
              const inMonth = day.getMonth() === month.getMonth();
              return (
                <button
                  key={value}
                  className={[
                    !inMonth ? "is-outside" : "",
                    value === selectedDate ? "is-selected" : "",
                    value === isoDate(today) ? "is-today" : ""
                  ].join(" ")}
                  onClick={() => {
                    setSelectedDate(value);
                    if (!inMonth) {
                      setMonth(new Date(day.getFullYear(), day.getMonth(), 1));
                    }
                  }}
                >
                  <span>{day.getDate()}</span>
                  <span className="calendar-markers">
                    {archiveDates.has(value) && <i title="已有归档" />}
                    {(taskCounts.get(value) ?? 0) > 0 && (
                      <b title={`${taskCounts.get(value)} 项安排`}>{taskCounts.get(value)}</b>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="archive-month-summary">
            <CalendarDays size={16} />
            <strong>{archives.length}</strong>
            <span>个归档 · {tasks.filter((task) => taskDate(task)?.startsWith(`${month.getFullYear()}-${String(month.getMonth() + 1).padStart(2, "0")}`)).length} 项安排</span>
          </div>
        </section>

        <section className="archive-editor">
          <header className="archive-editor-heading">
            <div>
              <span>{selectedDate}</span>
              <h2>{selectedArchive ? "日归档" : "尚未归档"}</h2>
            </div>
            <div className="archive-editor-actions">
              {selectedArchive && (
                <button
                  className="icon-button"
                  title="重新生成归档"
                  onClick={() => void mutate(() => api.sealDay(selectedDate, true))}
                >
                  <RotateCcw size={16} />
                </button>
              )}
              <button
                className="primary-button"
                disabled={busy}
                onClick={() =>
                  selectedArchive
                    ? saveArchive()
                    : void mutate(() => api.sealDay(selectedDate))
                }
              >
                {selectedArchive ? <Save size={15} /> : <Sparkles size={15} />}
                {selectedArchive ? "保存" : "生成归档"}
              </button>
            </div>
          </header>

          <section className="day-agenda">
            <header>
              <CalendarClock size={15} />
              <strong>当天安排</strong>
              <span>{selectedTasks.length}</span>
            </header>
            <div className="day-task-list">
              {selectedTasks.map((task) => (
                <button key={task.task_id} onClick={() => toggleTask(task)}>
                  {task.status === "completed" ? <CheckCircle2 size={15} /> : <Circle size={15} />}
                  <span className={task.status === "completed" ? "is-done" : ""}>
                    <strong>{task.title}</strong>
                    <small>{task.due_at ? new Date(task.due_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : ""} · P{task.priority}</small>
                  </span>
                </button>
              ))}
              {selectedTasks.length === 0 && <span className="day-empty">当天没有安排</span>}
            </div>
            <header className="day-note-heading">
              <FileText size={15} />
              <strong>当天笔记</strong>
              <span>{notes.length}</span>
            </header>
            <div className="day-notes">
              {notes.map((note) => (
                <article key={note.note_id}>
                  <strong>{note.title}</strong>
                  {note.content && <p>{note.content}</p>}
                </article>
              ))}
            </div>
            <form
              className="day-note-create"
              onSubmit={(event) => {
                event.preventDefault();
                createDailyNote();
              }}
            >
              <input value={noteTitle} onChange={(event) => setNoteTitle(event.target.value)} placeholder="记录标题" />
              <input value={noteContent} onChange={(event) => setNoteContent(event.target.value)} placeholder="补充内容（可选）" />
              <button className="icon-button" title="添加当天笔记" disabled={!noteTitle.trim() || busy}>
                <Plus size={15} />
              </button>
            </form>
          </section>

          <label className="form-field">
            <span>摘要</span>
            <textarea
              rows={3}
              value={summary}
              onChange={(event) => setSummary(event.target.value)}
              disabled={!selectedArchive}
            />
          </label>
          <label className="form-field diary-field">
            <span><BookOpenText size={14} /> 日记</span>
            <textarea
              rows={9}
              value={diary}
              onChange={(event) => setDiary(event.target.value)}
              disabled={!selectedArchive}
            />
          </label>
          <div className="archive-columns">
            <label className="form-field">
              <span>亮点</span>
              <textarea
                rows={5}
                value={highlights}
                onChange={(event) => setHighlights(event.target.value)}
                disabled={!selectedArchive}
              />
            </label>
            <label className="form-field">
              <span>决定</span>
              <textarea
                rows={5}
                value={decisions}
                onChange={(event) => setDecisions(event.target.value)}
                disabled={!selectedArchive}
              />
            </label>
            <label className="form-field">
              <span>未闭环</span>
              <textarea
                rows={5}
                value={openLoops}
                onChange={(event) => setOpenLoops(event.target.value)}
                disabled={!selectedArchive}
              />
            </label>
          </div>
        </section>
      </div>
    </section>
  );
}

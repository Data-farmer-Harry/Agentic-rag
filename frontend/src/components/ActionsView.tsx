import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  Archive,
  Activity,
  CalendarClock,
  Check,
  CheckCircle2,
  ChevronRight,
  Circle,
  ClipboardList,
  FileText,
  Inbox,
  ListChecks,
  LoaderCircle,
  Plus,
  Save,
  Target,
  TriangleAlert
} from "lucide-react";
import { api } from "../api";
import type {
  ChecklistItem,
  PersonalNote,
  PersonalPlan,
  PersonalPlanStep,
  PersonalTask,
  TaskStatus
} from "../types";

const taskStatusLabels: Record<TaskStatus, string> = {
  inbox: "收件箱",
  planned: "已规划",
  in_progress: "进行中",
  blocked: "受阻",
  completed: "已完成",
  archived: "已归档"
};

interface ActionsViewProps {
  focusedTaskId?: string;
  onChanged?: () => void;
}

export function ActionsView({ focusedTaskId, onChanged }: ActionsViewProps) {
  const [tasks, setTasks] = useState<PersonalTask[]>([]);
  const [plans, setPlans] = useState<PersonalPlan[]>([]);
  const [steps, setSteps] = useState<PersonalPlanStep[]>([]);
  const [checklist, setChecklist] = useState<ChecklistItem[]>([]);
  const [notes, setNotes] = useState<PersonalNote[]>([]);
  const [selectedTaskId, setSelectedTaskId] = useState<string>();
  const [selectedPlanId, setSelectedPlanId] = useState<string>();
  const [filter, setFilter] = useState<"open" | "all" | "done">("open");
  const [taskTitle, setTaskTitle] = useState("");
  const [taskPriority, setTaskPriority] = useState(3);
  const [taskDue, setTaskDue] = useState("");
  const [planTitle, setPlanTitle] = useState("");
  const [stepTitle, setStepTitle] = useState("");
  const [checkLabel, setCheckLabel] = useState("");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();

  const selectedTask = tasks.find((task) => task.task_id === selectedTaskId);
  const selectedPlan = plans.find((plan) => plan.plan_id === selectedPlanId);
  const visibleTasks = useMemo(() => {
    if (filter === "done") return tasks.filter((task) => task.status === "completed");
    if (filter === "open") {
      return tasks.filter(
        (task) => task.status !== "completed" && task.status !== "archived"
      );
    }
    return tasks.filter((task) => task.status !== "archived");
  }, [filter, tasks]);

  useEffect(() => {
    if (selectedTaskId && visibleTasks.some((task) => task.task_id === selectedTaskId)) return;
    setSelectedTaskId(visibleTasks[0]?.task_id);
  }, [selectedTaskId, visibleTasks]);

  const loadBase = useCallback(async () => {
    const [nextTasks, nextPlans] = await Promise.all([
      api.personalTasks(),
      api.personalPlans()
    ]);
    setTasks(nextTasks);
    setPlans(nextPlans);
    setSelectedTaskId((current) => {
      if (focusedTaskId && nextTasks.some((item) => item.task_id === focusedTaskId)) {
        return focusedTaskId;
      }
      return current && nextTasks.some((item) => item.task_id === current)
        ? current
        : nextTasks[0]?.task_id;
    });
    setSelectedPlanId((current) =>
      current && nextPlans.some((item) => item.plan_id === current)
        ? current
        : nextPlans[0]?.plan_id
    );
  }, [focusedTaskId]);

  const loadTaskDetails = useCallback(async (taskId?: string) => {
    if (!taskId) {
      setChecklist([]);
      setNotes([]);
      return;
    }
    const [nextChecklist, nextNotes] = await Promise.all([
      api.checklist(taskId),
      api.personalNotes(taskId)
    ]);
    setChecklist(nextChecklist);
    setNotes(nextNotes);
  }, []);

  const loadPlanSteps = useCallback(async (planId?: string) => {
    setSteps(planId ? await api.planSteps(planId) : []);
  }, []);

  useEffect(() => {
    void loadBase().catch((cause) =>
      setError(cause instanceof Error ? cause.message : "无法载入行动数据")
    );
  }, [loadBase]);

  useEffect(() => {
    void loadTaskDetails(selectedTaskId).catch((cause) =>
      setError(cause instanceof Error ? cause.message : "无法载入任务明细")
    );
  }, [loadTaskDetails, selectedTaskId]);

  useEffect(() => {
    void loadPlanSteps(selectedPlanId).catch((cause) =>
      setError(cause instanceof Error ? cause.message : "无法载入计划步骤")
    );
  }, [loadPlanSteps, selectedPlanId]);

  async function mutate(operation: () => Promise<unknown>, refresh: () => Promise<void>) {
    setBusy(true);
    setError(undefined);
    try {
      await operation();
      await refresh();
      onChanged?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  function createTask(event: FormEvent) {
    event.preventDefault();
    const title = taskTitle.trim();
    if (!title) return;
    void mutate(
      () =>
        api.createPersonalTask({
          title,
          priority: taskPriority,
          due_at: taskDue ? new Date(`${taskDue}T18:00:00`).toISOString() : undefined
        }),
      async () => {
        setTaskTitle("");
        setTaskDue("");
        await loadBase();
      }
    );
  }

  function setTaskStatus(task: PersonalTask, status: TaskStatus) {
    void mutate(
      () =>
        api.updatePersonalTask(task.task_id, {
          status,
          expected_version: task.version
        }),
      loadBase
    );
  }

  function createPlan(event: FormEvent) {
    event.preventDefault();
    const title = planTitle.trim();
    if (!title) return;
    void mutate(
      () => api.createPersonalPlan({ title, task_id: selectedTaskId }),
      async () => {
        setPlanTitle("");
        await loadBase();
      }
    );
  }

  function createStep(event: FormEvent) {
    event.preventDefault();
    const title = stepTitle.trim();
    if (!title || !selectedPlanId) return;
    void mutate(
      () => api.createPlanStep(selectedPlanId, { title }),
      async () => {
        setStepTitle("");
        await loadPlanSteps(selectedPlanId);
      }
    );
  }

  function toggleStep(step: PersonalPlanStep) {
    void mutate(
      () =>
        api.updatePlanStep(step.step_id, {
          status: step.status === "completed" ? "todo" : "completed",
          expected_version: step.version
        }),
      () => loadPlanSteps(selectedPlanId)
    );
  }

  function createChecklistItem(event: FormEvent) {
    event.preventDefault();
    const label = checkLabel.trim();
    if (!label || !selectedTaskId) return;
    void mutate(
      () => api.createChecklistItem({ task_id: selectedTaskId, label }),
      async () => {
        setCheckLabel("");
        await loadTaskDetails(selectedTaskId);
      }
    );
  }

  function toggleChecklist(item: ChecklistItem) {
    void mutate(
      () =>
        api.updateChecklistItem(item.item_id, {
          checked: !item.checked,
          expected_version: item.version
        }),
      () => loadTaskDetails(selectedTaskId)
    );
  }

  function saveNote(event: FormEvent) {
    event.preventDefault();
    const title = noteTitle.trim();
    if (!title || !selectedTaskId) return;
    void mutate(
      () =>
        api.upsertPersonalNote({
          title,
          content: noteContent,
          kind: "task",
          task_id: selectedTaskId
        }),
      async () => {
        setNoteTitle("");
        setNoteContent("");
        await loadTaskDetails(selectedTaskId);
      }
    );
  }

  return (
    <section className="data-view personal-view actions-view">
      <header className="view-header">
        <div>
          <span className="eyebrow">Personal control plane</span>
          <h1>行动中心</h1>
        </div>
        {busy && <LoaderCircle className="spin" size={17} />}
      </header>

      {error && <div className="error-banner">{error}</div>}

      <div className="action-summary" aria-label="任务概览">
        <div>
          <span className="summary-icon tone-blue"><Inbox size={16} /></span>
          <span><small>待处理</small><strong>{tasks.filter((task) => task.status === "inbox" || task.status === "planned").length}</strong></span>
        </div>
        <div>
          <span className="summary-icon tone-green"><Activity size={16} /></span>
          <span><small>进行中</small><strong>{tasks.filter((task) => task.status === "in_progress").length}</strong></span>
        </div>
        <div>
          <span className="summary-icon tone-amber"><TriangleAlert size={16} /></span>
          <span><small>受阻</small><strong>{tasks.filter((task) => task.status === "blocked").length}</strong></span>
        </div>
        <div>
          <span className="summary-icon tone-violet"><CheckCircle2 size={16} /></span>
          <span><small>已完成</small><strong>{tasks.filter((task) => task.status === "completed").length}</strong></span>
        </div>
      </div>

      <div className="action-command-band">
        <form className="inline-create-form" onSubmit={createTask}>
          <Plus size={16} />
          <input
            aria-label="新任务标题"
            value={taskTitle}
            onChange={(event) => setTaskTitle(event.target.value)}
            placeholder="添加一个任务"
          />
          <select
            aria-label="优先级"
            value={taskPriority}
            onChange={(event) => setTaskPriority(Number(event.target.value))}
          >
            <option value={1}>P1</option>
            <option value={2}>P2</option>
            <option value={3}>P3</option>
            <option value={4}>P4</option>
            <option value={5}>P5</option>
          </select>
          <input
            aria-label="截止日期"
            type="date"
            value={taskDue}
            onChange={(event) => setTaskDue(event.target.value)}
          />
          <button className="primary-button" disabled={busy || !taskTitle.trim()}>
            <Check size={15} />
            添加
          </button>
        </form>
      </div>

      <div className="personal-split">
        <div className="task-pane">
          <div className="pane-toolbar">
            <div className="segmented-control">
              {(["open", "all", "done"] as const).map((item) => (
                <button
                  key={item}
                  className={filter === item ? "is-selected" : ""}
                  onClick={() => setFilter(item)}
                >
                  {item === "open" ? "未完成" : item === "done" ? "已完成" : "全部"}
                </button>
              ))}
            </div>
            <span>{visibleTasks.length} 项</span>
          </div>
          <div className="task-list">
            {visibleTasks.map((task) => (
              <button
                className={`task-row ${selectedTaskId === task.task_id ? "is-selected" : ""}`}
                key={task.task_id}
                onClick={() => setSelectedTaskId(task.task_id)}
              >
                <span
                  className="task-check"
                  role="button"
                  tabIndex={0}
                  title={task.status === "completed" ? "恢复任务" : "完成任务"}
                  onClick={(event) => {
                    event.stopPropagation();
                    setTaskStatus(
                      task,
                      task.status === "completed" ? "in_progress" : "completed"
                    );
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.stopPropagation();
                      setTaskStatus(task, "completed");
                    }
                  }}
                >
                  {task.status === "completed" ? (
                    <CheckCircle2 size={17} />
                  ) : (
                    <Circle size={17} />
                  )}
                </span>
                <span className="task-row-copy">
                  <strong>{task.title}</strong>
                  <small>
                    P{task.priority} · {taskStatusLabels[task.status]}
                    {task.due_at
                      ? ` · ${new Date(task.due_at).toLocaleDateString("zh-CN")}`
                      : ""}
                  </small>
                </span>
                <ChevronRight size={15} />
              </button>
            ))}
            {visibleTasks.length === 0 && (
              <div className="pane-empty">
                <ClipboardList size={20} />
                <span>当前列表为空</span>
              </div>
            )}
          </div>
        </div>

        <div className="detail-pane">
          {selectedTask ? (
            <>
              <div className="detail-heading">
                <div>
                  <span className={`status-chip status-${selectedTask.status}`}>
                    {taskStatusLabels[selectedTask.status]}
                  </span>
                  <h2>{selectedTask.title}</h2>
                </div>
                <button
                  className="icon-button danger"
                  title="归档任务"
                  onClick={() => setTaskStatus(selectedTask, "archived")}
                >
                  <Archive size={16} />
                </button>
              </div>
              {selectedTask.description && (
                <p className="detail-description">{selectedTask.description}</p>
              )}

              <section className="detail-section">
                <header>
                  <ListChecks size={15} />
                  <strong>检查项</strong>
                  <span>{checklist.filter((item) => item.checked).length}/{checklist.length}</span>
                </header>
                <div className="compact-list">
                  {checklist.map((item) => (
                    <button key={item.item_id} onClick={() => toggleChecklist(item)}>
                      {item.checked ? <CheckCircle2 size={15} /> : <Circle size={15} />}
                      <span className={item.checked ? "is-done" : ""}>{item.label}</span>
                    </button>
                  ))}
                </div>
                <form className="compact-create" onSubmit={createChecklistItem}>
                  <input
                    value={checkLabel}
                    onChange={(event) => setCheckLabel(event.target.value)}
                    placeholder="添加检查项"
                  />
                  <button className="icon-button" title="添加检查项" disabled={!checkLabel.trim()}>
                    <Plus size={15} />
                  </button>
                </form>
              </section>

              <section className="detail-section">
                <header>
                  <Target size={15} />
                  <strong>计划</strong>
                  <span>{plans.filter((plan) => plan.task_id === selectedTaskId).length}</span>
                </header>
                <form className="compact-create" onSubmit={createPlan}>
                  <input
                    value={planTitle}
                    onChange={(event) => setPlanTitle(event.target.value)}
                    placeholder="为当前任务创建计划"
                  />
                  <button className="icon-button" title="创建计划" disabled={!planTitle.trim()}>
                    <Plus size={15} />
                  </button>
                </form>
                <div className="plan-selector">
                  {plans.map((plan) => (
                    <button
                      key={plan.plan_id}
                      className={selectedPlanId === plan.plan_id ? "is-selected" : ""}
                      onClick={() => setSelectedPlanId(plan.plan_id)}
                    >
                      <span>{plan.title}</span>
                      <small>{plan.status}</small>
                    </button>
                  ))}
                </div>
                {selectedPlan && (
                  <div className="plan-detail">
                    <div className="plan-status-row">
                      <strong>{selectedPlan.title}</strong>
                      <select
                        value={selectedPlan.status}
                        onChange={(event) =>
                          void mutate(
                            () =>
                              api.updatePersonalPlan(selectedPlan.plan_id, {
                                status: event.target.value,
                                expected_version: selectedPlan.version
                              }),
                            loadBase
                          )
                        }
                      >
                        <option value="draft">草稿</option>
                        <option value="active">执行中</option>
                        <option value="paused">暂停</option>
                        <option value="completed">完成</option>
                        <option value="archived">归档</option>
                      </select>
                    </div>
                    <div className="compact-list">
                      {steps.map((step) => (
                        <button key={step.step_id} onClick={() => toggleStep(step)}>
                          {step.status === "completed" ? (
                            <CheckCircle2 size={15} />
                          ) : (
                            <Circle size={15} />
                          )}
                          <span className={step.status === "completed" ? "is-done" : ""}>
                            {step.title}
                          </span>
                        </button>
                      ))}
                    </div>
                    <form className="compact-create" onSubmit={createStep}>
                      <input
                        value={stepTitle}
                        onChange={(event) => setStepTitle(event.target.value)}
                        placeholder="添加计划步骤"
                      />
                      <button className="icon-button" title="添加步骤" disabled={!stepTitle.trim()}>
                        <Plus size={15} />
                      </button>
                    </form>
                  </div>
                )}
              </section>

              <section className="detail-section">
                <header>
                  <FileText size={15} />
                  <strong>任务笔记</strong>
                  <span>{notes.length}</span>
                </header>
                <div className="note-history">
                  {notes.map((note) => (
                    <div key={note.note_id}>
                      <strong>{note.title}</strong>
                      <p>{note.content}</p>
                    </div>
                  ))}
                </div>
                <form className="note-form" onSubmit={saveNote}>
                  <input
                    value={noteTitle}
                    onChange={(event) => setNoteTitle(event.target.value)}
                    placeholder="笔记标题"
                  />
                  <textarea
                    value={noteContent}
                    onChange={(event) => setNoteContent(event.target.value)}
                    placeholder="记录上下文、判断或下一步"
                    rows={3}
                  />
                  <button className="text-button" disabled={!noteTitle.trim()}>
                    <Save size={14} />
                    保存笔记
                  </button>
                </form>
              </section>
            </>
          ) : (
            <div className="pane-empty detail-empty">
              <CalendarClock size={22} />
              <span>选择一个任务查看详情</span>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

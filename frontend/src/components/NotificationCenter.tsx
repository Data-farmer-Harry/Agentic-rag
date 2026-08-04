import { useEffect, useRef, useState } from "react";
import {
  Bell,
  BellRing,
  Check,
  CheckCheck,
  ChevronRight,
  Clock3,
  RefreshCw
} from "lucide-react";
import type { ReminderKind, TaskReminder, TaskReminderFeed } from "../types";

const reminderLabels: Record<ReminderKind, string> = {
  overdue: "已逾期",
  due_soon: "即将到期",
  today: "今天"
};

interface NotificationCenterProps {
  feed?: TaskReminderFeed;
  refreshing: boolean;
  desktopPermission: NotificationPermission | "unsupported";
  onRefresh: () => Promise<void>;
  onMarkRead: (taskId: string) => Promise<void>;
  onMarkAllRead: () => Promise<void>;
  onSnooze: (taskId: string) => Promise<void>;
  onOpenTask: (taskId: string) => void;
  onEnableDesktop: () => Promise<void>;
}

function formatDue(reminder: TaskReminder, timezone?: string) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: timezone
    }).format(new Date(reminder.due_at));
  } catch {
    return new Date(reminder.due_at).toLocaleString("zh-CN", {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    });
  }
}

export function NotificationCenter({
  feed,
  refreshing,
  desktopPermission,
  onRefresh,
  onMarkRead,
  onMarkAllRead,
  onSnooze,
  onOpenTask,
  onEnableDesktop
}: NotificationCenterProps) {
  const [open, setOpen] = useState(false);
  const [busyTaskId, setBusyTaskId] = useState<string>();
  const [error, setError] = useState<string>();
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function closeOnPointer(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("pointerdown", closeOnPointer);
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("pointerdown", closeOnPointer);
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, []);

  async function runTaskAction(taskId: string, action: () => Promise<void>) {
    setBusyTaskId(taskId);
    setError(undefined);
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提醒操作失败");
    } finally {
      setBusyTaskId(undefined);
    }
  }

  async function runPanelAction(action: () => Promise<void>) {
    setError(undefined);
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提醒操作失败");
    }
  }

  function openTask(reminder: TaskReminder) {
    onOpenTask(reminder.task_id);
    setOpen(false);
    if (reminder.unread) {
      void onMarkRead(reminder.task_id).catch((cause) =>
        console.error("Unable to mark opened reminder as read", cause)
      );
    }
  }

  return (
    <div className="notification-center" ref={rootRef}>
      <button
        className={`icon-button notification-trigger ${open ? "is-active" : ""}`}
        title="任务提醒"
        aria-label={`任务提醒，${feed?.unread_count ?? 0} 条未读`}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <Bell size={17} />
        {!!feed?.unread_count && (
          <span className="notification-count">
            {feed.unread_count > 9 ? "9+" : feed.unread_count}
          </span>
        )}
      </button>

      {open && (
        <section className="notification-panel" aria-label="任务提醒列表">
          <header className="notification-header">
            <div>
              <strong>任务提醒</strong>
              <span>{feed?.items.length ?? 0} 项</span>
            </div>
            <div className="notification-header-actions">
              {!!feed?.unread_count && (
                <button
                  title="全部标为已读"
                  onClick={() => void runPanelAction(onMarkAllRead)}
                >
                  <CheckCheck size={16} />
                </button>
              )}
              <button title="刷新提醒" onClick={() => void onRefresh()}>
                <RefreshCw size={16} className={refreshing ? "spin" : ""} />
              </button>
            </div>
          </header>

          <div className="notification-list">
            {error && <div className="notification-error" role="alert">{error}</div>}
            {feed?.items.map((reminder) => (
              <article
                className={`notification-item ${reminder.unread ? "is-unread" : ""}`}
                key={`${reminder.task_id}:${reminder.due_at}`}
              >
                <button className="notification-copy" onClick={() => openTask(reminder)}>
                  <span className={`reminder-kind kind-${reminder.kind}`}>
                    {reminderLabels[reminder.kind]}
                  </span>
                  <strong>{reminder.title}</strong>
                  <small>{formatDue(reminder, feed.timezone)} · P{reminder.priority}</small>
                </button>
                <div className="notification-item-actions">
                  {reminder.unread && (
                    <button
                      title="标为已读"
                      disabled={busyTaskId === reminder.task_id}
                      onClick={() => void runTaskAction(
                        reminder.task_id,
                        () => onMarkRead(reminder.task_id)
                      )}
                    >
                      <Check size={15} />
                    </button>
                  )}
                  <button
                    title="1 小时后提醒"
                    disabled={busyTaskId === reminder.task_id}
                    onClick={() => void runTaskAction(
                      reminder.task_id,
                      () => onSnooze(reminder.task_id)
                    )}
                  >
                    <Clock3 size={15} />
                  </button>
                  <button title="打开任务" onClick={() => openTask(reminder)}>
                    <ChevronRight size={15} />
                  </button>
                </div>
              </article>
            ))}
            {feed && feed.items.length === 0 && (
              <div className="notification-empty">
                <BellRing size={20} />
                <span>暂无到期提醒</span>
              </div>
            )}
            {!feed && (
              <div className="notification-empty">
                <RefreshCw className="spin" size={19} />
                <span>正在载入</span>
              </div>
            )}
          </div>

          {desktopPermission === "default" && (
            <footer className="notification-footer">
              <button onClick={() => void runPanelAction(onEnableDesktop)}>
                <BellRing size={15} />
                开启桌面提醒
              </button>
            </footer>
          )}
        </section>
      )}
    </div>
  );
}

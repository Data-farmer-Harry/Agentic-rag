import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  CalendarDays,
  Database,
  Files,
  GitBranch,
  History,
  ListTodo,
  MessageSquarePlus,
  MessageSquareText,
  Network,
  RefreshCw,
  Search,
  Sparkles,
  UserRound
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { ConversationSummary } from "../types";
import type { ViewName } from "./Sidebar";

interface CommandItem {
  id: string;
  label: string;
  detail: string;
  icon: LucideIcon;
  keywords: string;
  run: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  onNavigate: (view: ViewName) => void;
  onRefresh: () => void;
  onNewConversation: () => void;
  conversations: ConversationSummary[];
  onConversationChange: (sessionId: string) => void;
}

const viewCommands: Array<[ViewName, string, string, LucideIcon, string]> = [
  ["chat", "对话", "询问架构、事故、决策或知识", MessageSquareText, "chat 对话 agent"],
  ["knowledge", "知识", "导入、管理和查看知识来源", Files, "rag document 知识 文档"],
  ["graph", "系统地图", "查看服务、团队、事故与决策关系", Network, "kg graph 图谱 实体 关系 系统地图"],
  ["actions", "工作", "任务、计划、检查项与工作记录", ListTodo, "task plan 行动 工作 任务 计划"],
  ["learning", "学习", "我的记忆、系统学到与待确认建议", GitBranch, "learning memory evolution 学习 记忆"],
  ["runs", "运行", "执行轨迹、技能与审计详情", History, "run trace 运行 轨迹 skill"],
  ["review", "日历回顾", "日记与每日归档", CalendarDays, "calendar diary 回顾 日历"],
  ["memory", "我的记忆", "长期记忆、纠正与撤销", Database, "memory 记忆"],
  ["skills", "技能治理", "技能版本、评测与健康度", Sparkles, "skill 技能"],
  ["profile", "工作区设置", "Persona、偏好与情绪", UserRound, "profile persona emotion 个人 设置"]
];

export function CommandPalette({
  open,
  onClose,
  onNavigate,
  onRefresh,
  onNewConversation,
  conversations,
  onConversationChange
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const commands = useMemo<CommandItem[]>(
    () => [
      {
        id: "new-conversation",
        label: "新建对话",
        detail: "清空当前消息并开始新任务",
        icon: MessageSquarePlus,
        keywords: "new chat 新建 对话",
        run: onNewConversation
      },
      {
        id: "refresh",
        label: "刷新工作区",
        detail: "同步最新运行、知识与学习状态",
        icon: RefreshCw,
        keywords: "refresh sync 刷新 同步",
        run: onRefresh
      },
      ...viewCommands.map(([id, label, detail, icon, keywords]) => ({
        id: `view-${id}`,
        label,
        detail,
        icon,
        keywords,
        run: () => onNavigate(id)
      }))
    ],
    [onNavigate, onNewConversation, onRefresh]
  );

  const conversationCommands = useMemo<CommandItem[]>(
    () => conversations
      .filter((conversation) => !conversation.archived)
      .map((conversation) => {
        const updated = new Intl.DateTimeFormat("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit"
        }).format(new Date(conversation.updated_at));
        return {
          id: `conversation-${conversation.session_id}`,
          label: conversation.title,
          detail: `历史对话 · ${updated}${conversation.preview ? ` · ${conversation.preview}` : ""}`,
          icon: MessageSquareText,
          keywords: `history session 历史 会话 ${conversation.preview}`,
          run: () => onConversationChange(conversation.session_id)
        };
      }),
    [conversations, onConversationChange]
  );

  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return commands;
    return [...commands, ...conversationCommands].filter((command) =>
      `${command.label} ${command.detail} ${command.keywords}`.toLocaleLowerCase().includes(normalized)
    );
  }, [commands, conversationCommands, query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    setActiveIndex((current) => Math.min(current, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  if (!open) return null;

  function execute(command?: CommandItem) {
    if (!command) return;
    command.run();
    onClose();
  }

  return (
    <div className="command-layer" role="presentation" onMouseDown={onClose}>
      <section
        className="command-palette"
        role="dialog"
        aria-modal="true"
        aria-label="搜索或执行命令"
        onMouseDown={(event) => event.stopPropagation()}
          >
        <div className="command-input">
          <Search size={19} />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((current) => Math.min(current + 1, filtered.length - 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((current) => Math.max(current - 1, 0));
              }
              if (event.key === "Enter") {
                event.preventDefault();
                execute(filtered[activeIndex]);
              }
              if (event.key === "Escape") onClose();
            }}
            placeholder="搜索页面、操作或历史对话"
            aria-label="搜索页面、操作或历史对话"
          />
        </div>
        <div className="command-results" role="listbox">
          {filtered.map((command, index) => {
            const Icon = command.icon;
            return (
              <button
                key={command.id}
                className={index === activeIndex ? "is-active" : ""}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => execute(command)}
                role="option"
                aria-selected={index === activeIndex}
              >
                <span className="command-icon"><Icon size={17} /></span>
                <span className="command-copy">
                  <strong>{command.label}</strong>
                  <small>{command.detail}</small>
                </span>
                <ArrowRight size={15} />
              </button>
            );
          })}
          {filtered.length === 0 && <div className="command-empty">没有匹配的页面、操作或历史对话</div>}
        </div>
      </section>
    </div>
  );
}

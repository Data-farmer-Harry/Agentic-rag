import { useState } from "react";
import {
  BrainCircuit,
  CalendarDays,
  ChevronDown,
  Database,
  Files,
  GitBranch,
  History,
  ListTodo,
  MessageSquareText,
  MoreHorizontal,
  Network,
  Sparkles,
  UserRound,
  X
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { Overview } from "../types";

export type ViewName =
  | "chat"
  | "runs"
  | "knowledge"
  | "graph"
  | "memory"
  | "actions"
  | "review"
  | "profile"
  | "skills"
  | "learning";

export interface NavigationItem {
  id: ViewName;
  label: string;
  icon: LucideIcon;
  count?: "runs" | "documents" | "memories" | "skills" | "change_sets";
}

export const navigationGroups: Array<{ label: string; items: NavigationItem[] }> = [
  {
    label: "工作区",
    items: [
      { id: "chat", label: "对话", icon: MessageSquareText },
      { id: "knowledge", label: "知识", icon: Files, count: "documents" },
      { id: "graph", label: "系统地图", icon: Network },
      { id: "actions", label: "工作", icon: ListTodo },
      { id: "learning", label: "学习", icon: GitBranch, count: "change_sets" },
      { id: "runs", label: "运行", icon: History, count: "runs" }
    ]
  }
];

const mobilePrimary: NavigationItem[] = [
  { id: "chat", label: "对话", icon: MessageSquareText },
  { id: "knowledge", label: "知识", icon: Files },
  { id: "graph", label: "地图", icon: Network },
  { id: "actions", label: "工作", icon: ListTodo }
];

const mobileSecondary: NavigationItem[] = [
  { id: "learning", label: "学习", icon: GitBranch, count: "change_sets" },
  { id: "runs", label: "运行", icon: History, count: "runs" },
  { id: "memory", label: "我的记忆", icon: Database, count: "memories" },
  { id: "review", label: "日历回顾", icon: CalendarDays },
  { id: "skills", label: "技能治理", icon: Sparkles, count: "skills" },
  { id: "profile", label: "工作区设置", icon: UserRound }
];

interface SidebarProps {
  view: ViewName;
  onViewChange: (view: ViewName) => void;
  overview?: Overview;
}

export function Sidebar({ view, onViewChange, overview }: SidebarProps) {
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  const [desktopMoreOpen, setDesktopMoreOpen] = useState(false);
  const documentCount = overview?.counts.documents ?? 0;
  const chunkCount = overview?.counts.chunks ?? 0;
  const knowledgeReady = documentCount > 0 && chunkCount > 0;

  function selectView(next: ViewName) {
    setMobileMoreOpen(false);
    setDesktopMoreOpen(false);
    onViewChange(next);
  }

  return (
    <>
      <aside className="sidebar">
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">
            <BrainCircuit size={20} />
          </div>
          <div className="brand-copy">
            <strong>HermesGraph</strong>
            <span>Engineering intelligence</span>
          </div>
          <ChevronDown className="brand-chevron" size={15} aria-hidden="true" />
        </div>

        <nav className="primary-nav desktop-navigation" aria-label="主导航">
          {navigationGroups.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-group-label">{group.label}</span>
              {group.items.map((item) => {
                const Icon = item.icon;
                const count = item.count && overview ? overview.counts[item.count] : undefined;
                return (
                  <button
                    key={item.id}
                    className={`nav-item ${view === item.id ? "is-active" : ""}`}
                    onClick={() => selectView(item.id)}
                    title={item.label}
                    aria-current={view === item.id ? "page" : undefined}
                  >
                    <Icon size={17} strokeWidth={1.8} />
                    <span className="nav-label">{item.label}</span>
                    {count !== undefined && <span className="nav-count">{count}</span>}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="sidebar-advanced">
          <button
            className={`sidebar-advanced-trigger ${mobileSecondary.some((item) => item.id === view) ? "is-active" : ""}`}
            onClick={() => setDesktopMoreOpen((current) => !current)}
            aria-expanded={desktopMoreOpen}
          >
            <MoreHorizontal size={17} />
            <span>更多工具</span>
            <ChevronDown size={14} aria-hidden="true" />
          </button>
          {desktopMoreOpen && (
            <div className="sidebar-advanced-menu" role="menu" aria-label="更多工具">
              {mobileSecondary.map((item) => {
                const Icon = item.icon;
                const count = item.count && overview ? overview.counts[item.count] : undefined;
                return (
                  <button key={item.id} role="menuitem" onClick={() => selectView(item.id)}>
                    <Icon size={15} />
                    <span>{item.label}</span>
                    {count !== undefined && <small>{count}</small>}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div className="sidebar-runtime">
          <span className={`runtime-dot ${knowledgeReady ? "is-live" : ""}`} />
          <div>
            <strong>{knowledgeReady ? "知识库已就绪" : "等待添加资料"}</strong>
            <span>
              {knowledgeReady
                ? `${documentCount} 份资料 · ${chunkCount} 个分块`
                : "从知识页添加研发资料"}
            </span>
          </div>
        </div>

        <nav className="mobile-navigation" aria-label="移动端主导航">
          {mobilePrimary.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={view === item.id ? "is-active" : ""}
                onClick={() => selectView(item.id)}
                aria-current={view === item.id ? "page" : undefined}
              >
                <Icon size={19} strokeWidth={1.8} />
                <span>{item.label}</span>
              </button>
            );
          })}
          <button
            className={mobileMoreOpen || mobileSecondary.some((item) => item.id === view) ? "is-active" : ""}
            onClick={() => setMobileMoreOpen((current) => !current)}
            aria-expanded={mobileMoreOpen}
          >
            <MoreHorizontal size={20} />
            <span>更多</span>
          </button>
        </nav>
      </aside>

      {mobileMoreOpen && (
        <div className="mobile-more-layer" role="presentation" onClick={() => setMobileMoreOpen(false)}>
          <div className="mobile-more-sheet" role="dialog" aria-label="更多导航" onClick={(event) => event.stopPropagation()}>
            <header>
              <strong>更多</strong>
              <button className="icon-button" onClick={() => setMobileMoreOpen(false)} title="关闭">
                <X size={18} />
              </button>
            </header>
            <div className="mobile-more-grid">
              {mobileSecondary.map((item) => {
                const Icon = item.icon;
                const count = item.count && overview ? overview.counts[item.count] : undefined;
                return (
                  <button
                    key={item.id}
                    className={view === item.id ? "is-active" : ""}
                    onClick={() => selectView(item.id)}
                  >
                    <Icon size={19} />
                    <span>{item.label}</span>
                    {count !== undefined && <small>{count}</small>}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

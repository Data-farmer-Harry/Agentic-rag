import type { ToolEvent } from "./types";

export function formatDuration(milliseconds: number) {
  if (milliseconds < 1_000) return `${Math.max(0, Math.round(milliseconds))} ms`;
  const seconds = Math.round(milliseconds / 100) / 10;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

export function describeTool(tool: ToolEvent) {
  const name = tool.tool_name.toLocaleLowerCase();
  if (name.includes("publish_answer")) {
    return { title: "整理并发布回答", detail: "回答已通过发布检查" };
  }
  if (name.includes("graph")) {
    return { title: "查询知识图谱", detail: "已完成实体、关系或路径检索" };
  }
  if (name.includes("web") || name.includes("search_online")) {
    return { title: "搜索公开网页", detail: "已完成受控网络检索" };
  }
  if (name.includes("retriev") || name.includes("knowledge") || name.includes("rag")) {
    return { title: "检索知识库", detail: "已搜索个人资料和参考知识" };
  }
  if (name.includes("computer") || name.includes("workspace") || name.includes("file")) {
    return { title: "读取工作区资料", detail: "已完成受控只读访问" };
  }
  if (name.includes("vision") || name.includes("image")) {
    return { title: "分析图片", detail: "已完成视觉内容分析" };
  }
  if (name.includes("memory")) {
    return { title: "处理长期记忆", detail: "已完成记忆读取或更新" };
  }
  if (name.includes("task") || name.includes("todo")) {
    return { title: "处理个人任务", detail: "已完成任务读取或更新" };
  }
  if (name.includes("plan")) {
    return { title: "处理个人计划", detail: "已完成计划读取或更新" };
  }
  if (name.includes("note")) {
    return { title: "处理个人笔记", detail: "已完成笔记读取或更新" };
  }
  if (name.includes("skill")) {
    return { title: "检查可用技能", detail: "已完成技能选择或更新" };
  }
  return { title: "执行受控工具", detail: "已完成一次能力调用" };
}

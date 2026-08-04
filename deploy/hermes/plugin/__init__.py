from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any
from urllib import error, parse, request

from .native_snapshots import (
    NativeSnapshotError,
    NativeSnapshotManager,
    start_admin_server,
)

TOOLSET = "hermesgraph_bridge"
_SNAPSHOT_MANAGER: NativeSnapshotManager | None = None
_ADMIN_SERVER: Any | None = None
_SENSITIVE_ARGUMENT_KEYS = {
    "content",
    "file_content",
    "new_string",
    "old_string",
    "old_text",
}


def _feature_enabled(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "disabled"}


def _post(path: str, payload: dict[str, Any]) -> str:
    base_url = os.environ["HERMESGRAPH_BRIDGE_URL"].rstrip("/")
    token = os.environ["HERMESGRAPH_BRIDGE_TOKEN"]
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{base_url}/{path.lstrip('/')}",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=90) as response:
            return response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        return json.dumps(
            {"success": False, "error": f"HermesGraph HTTP {exc.code}: {detail}"},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps(
            {"success": False, "error": f"HermesGraph bridge unavailable: {type(exc).__name__}"},
            ensure_ascii=False,
        )


def _tool_handler(tool_name: str):
    def handler(params: dict[str, Any], **kwargs: Any) -> str:
        task_id = str(kwargs.get("task_id") or "")
        if not task_id:
            return json.dumps({"success": False, "error": "Missing Hermes task_id"})
        encoded_task_id = parse.quote(task_id, safe="")
        return _post(f"runs/{encoded_task_id}/tools/{tool_name}", params)

    return handler


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _call_key(
    tool_name: str,
    args: dict[str, Any],
    *,
    task_id: str,
    tool_call_id: str,
    turn_id: str,
) -> str:
    if tool_call_id:
        return f"tool:{tool_call_id}"
    payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"fallback:{task_id}:{turn_id}:{tool_name}:{fingerprint}"


def _bridge_run_id(*, task_id: str, session_id: str) -> str | None:
    for candidate in (session_id, task_id):
        normalized = str(candidate or "")
        if normalized.startswith("hg_"):
            return normalized
    return None


def _is_background_review_thread() -> bool:
    return threading.current_thread().name == "bg-review"


def _redacted_value(value: Any) -> dict[str, Any]:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "redacted": True,
        "length": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _sanitize_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key in _SENSITIVE_ARGUMENT_KEYS:
        return _redacted_value(value)
    if depth >= 4:
        return _redacted_value(value)
    if isinstance(value, dict):
        return {
            str(child_key)[:128]: _sanitize_value(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child_value in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str) and len(value) > 500:
        return _redacted_value(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)[:500]


def _result_summary(result: Any) -> str:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return json.dumps(_redacted_value(result), ensure_ascii=False)
    if not isinstance(parsed, dict):
        return json.dumps(_redacted_value(parsed), ensure_ascii=False)
    summary: dict[str, Any] = {}
    for key in ("success", "staged", "done", "_archived"):
        if key in parsed:
            summary[key] = parsed[key]
    if "error" in parsed:
        summary["error"] = str(parsed["error"])[:500]
    return json.dumps(summary or {"result": "completed"}, ensure_ascii=False)


def _reported_applied(result: Any, status: str) -> bool:
    if status not in {"", "ok"}:
        return False
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return True
    if not isinstance(parsed, dict):
        return True
    return (
        parsed.get("success") is not False
        and not parsed.get("error")
        and not parsed.get("staged")
        and not parsed.get("pending_id")
    )


def _snapshot_manager() -> NativeSnapshotManager:
    global _SNAPSHOT_MANAGER
    if _SNAPSHOT_MANAGER is None:
        max_bytes = int(os.environ.get("HERMESGRAPH_NATIVE_SNAPSHOT_MAX_BYTES", "5000000"))
        max_total_bytes = int(
            os.environ.get("HERMESGRAPH_NATIVE_SNAPSHOT_MAX_TOTAL_BYTES", "1000000000")
        )
        terminal_retention_days = int(
            os.environ.get("HERMESGRAPH_NATIVE_SNAPSHOT_TERMINAL_RETENTION_DAYS", "7")
        )
        no_change_retention_hours = int(
            os.environ.get("HERMESGRAPH_NATIVE_SNAPSHOT_NO_CHANGE_RETENTION_HOURS", "24")
        )
        _SNAPSHOT_MANAGER = NativeSnapshotManager(
            home=os.path.abspath(os.environ.get("HERMES_HOME", "/data/hermes")),
            max_bytes=max_bytes,
            max_total_bytes=max_total_bytes,
            terminal_retention_days=terminal_retention_days,
            no_change_retention_hours=no_change_retention_hours,
        )
    return _SNAPSHOT_MANAGER


def _start_native_admin() -> None:
    global _ADMIN_SERVER
    if _ADMIN_SERVER is not None:
        return
    manager = _snapshot_manager()
    _ADMIN_SERVER = start_admin_server(
        manager,
        host=os.environ.get("HERMESGRAPH_NATIVE_ADMIN_HOST", "0.0.0.0"),
        port=int(os.environ.get("HERMESGRAPH_NATIVE_ADMIN_PORT", "8643")),
        token=os.environ.get("HERMESGRAPH_NATIVE_ADMIN_TOKEN", ""),
    )


def register(ctx: Any) -> None:
    _start_native_admin()
    ctx.register_tool(
        name="search_knowledge",
        toolset=TOOLSET,
        schema=_schema(
            "search_knowledge",
            "Search the scoped personal knowledge base with agentic hybrid retrieval.",
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query"],
        ),
        handler=_tool_handler("search_knowledge"),
    )
    if _feature_enabled("HERMESGRAPH_GRAPH_ENABLED", default=True):
        ctx.register_tool(
            name="search_graph",
            toolset=TOOLSET,
            schema=_schema(
                "search_graph",
                "Traverse the scoped evidence-backed knowledge graph using an "
                "allowlisted template.",
                {
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "template": {
                        "type": "string",
                        "enum": ["neighbors", "paths", "conflicts"],
                    },
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 3},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["entities"],
            ),
            handler=_tool_handler("search_graph"),
        )
        ctx.register_tool(
            name="resolve_graph_entities",
            toolset=TOOLSET,
            schema=_schema(
                "resolve_graph_entities",
                "Resolve canonical graph entities from names or aliases and return "
                "source-backed matches.",
                {
                    "mentions": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "entity_types": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        "maxItems": 10,
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                ["mentions"],
            ),
            handler=_tool_handler("resolve_graph_entities"),
        )
        ctx.register_tool(
            name="retrieve_evidence_subgraph",
            toolset=TOOLSET,
            schema=_schema(
                "retrieve_evidence_subgraph",
                "Fuse scoped vector retrieval with an evidence-backed multi-hop "
                "knowledge subgraph.",
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "seed_entities": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        "maxItems": 10,
                    },
                    "entity_types": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        "maxItems": 10,
                    },
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 3},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    "path_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["query"],
            ),
            handler=_tool_handler("retrieve_evidence_subgraph"),
        )
        ctx.register_tool(
            name="compare_graph_entities",
            toolset=TOOLSET,
            schema=_schema(
                "compare_graph_entities",
                "Compare two resolved entities through connecting paths and "
                "shared or exclusive evidence-backed neighbors.",
                {
                    "left_entity": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "right_entity": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 3},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["left_entity", "right_entity"],
            ),
            handler=_tool_handler("compare_graph_entities"),
        )
    if _feature_enabled("HERMESGRAPH_WEB_SEARCH_ENABLED", default=False):
        ctx.register_tool(
            name="search_web",
            toolset=TOOLSET,
            schema=_schema(
                "search_web",
                "Search the public web through HermesGraph and return run-scoped URL evidence.",
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                    },
                },
                ["query"],
            ),
            handler=_tool_handler("search_web"),
        )
    if _feature_enabled("HERMESGRAPH_COMPUTER_ENABLED", default=False):
        ctx.register_tool(
            name="list_workspace_files",
            toolset=TOOLSET,
            schema=_schema(
                "list_workspace_files",
                "List safe files under an explicitly allowlisted read-only workspace root.",
                {
                    "root": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]*$",
                        "maxLength": 64,
                    },
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "recursive": {"type": "boolean"},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                ["root"],
            ),
            handler=_tool_handler("list_workspace_files"),
        )
        ctx.register_tool(
            name="read_workspace_file",
            toolset=TOOLSET,
            schema=_schema(
                "read_workspace_file",
                "Read a bounded text, code, PDF, DOCX, or XLSX segment from an "
                "allowlisted read-only workspace and return citable evidence.",
                {
                    "root": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]*$",
                        "maxLength": 64,
                    },
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "start_line": {"type": "integer", "minimum": 1, "maximum": 10000000},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                ["root", "path"],
            ),
            handler=_tool_handler("read_workspace_file"),
        )
        ctx.register_tool(
            name="search_workspace_files",
            toolset=TOOLSET,
            schema=_schema(
                "search_workspace_files",
                "Search supported files in an allowlisted read-only workspace and "
                "return line-level citable evidence.",
                {
                    "root": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]*$",
                        "maxLength": 64,
                    },
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "case_sensitive": {"type": "boolean"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                    "max_files": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                ["root", "query"],
            ),
            handler=_tool_handler("search_workspace_files"),
        )
    if _feature_enabled("HERMESGRAPH_PERSONAL_ENABLED", default=True):
        ctx.register_tool(
            name="manage_personal_tasks",
            toolset=TOOLSET,
            schema=_schema(
                "manage_personal_tasks",
                "List, create, update, complete, or archive scoped personal tasks. "
                "Use writes only when the user asks for them.",
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "create",
                            "update",
                            "complete",
                            "archive",
                            "list_checklist",
                            "add_checklist",
                            "update_checklist",
                        ],
                    },
                    "task_id": {"type": "string", "format": "uuid"},
                    "item_id": {"type": "string", "format": "uuid"},
                    "checklist_item": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "minLength": 1, "maxLength": 500},
                            "position": {"type": "integer", "minimum": 0},
                        },
                        "required": ["label"],
                        "additionalProperties": False,
                    },
                    "checklist_patch": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "minLength": 1, "maxLength": 500},
                            "checked": {"type": "boolean"},
                            "position": {"type": "integer", "minimum": 0},
                            "expected_version": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                    "task": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "description": {"type": "string", "maxLength": 10000},
                            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                            "due_at": {"type": "string", "format": "date-time"},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 64},
                                "maxItems": 20,
                            },
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                    "patch": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "description": {"type": "string", "maxLength": 10000},
                            "status": {
                                "type": "string",
                                "enum": [
                                    "inbox",
                                    "planned",
                                    "in_progress",
                                    "blocked",
                                    "completed",
                                    "archived",
                                ],
                            },
                            "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                            "due_at": {"type": "string", "format": "date-time"},
                            "tags": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 64},
                                "maxItems": 20,
                            },
                            "expected_version": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                },
                ["action"],
            ),
            handler=_tool_handler("manage_personal_tasks"),
        )
        ctx.register_tool(
            name="manage_personal_plans",
            toolset=TOOLSET,
            schema=_schema(
                "manage_personal_plans",
                "List and evolve scoped plans and plan steps for explicit user goals.",
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "create",
                            "update",
                            "activate",
                            "pause",
                            "complete",
                            "archive",
                            "add_step",
                            "update_step",
                        ],
                    },
                    "plan_id": {"type": "string", "format": "uuid"},
                    "step_id": {"type": "string", "format": "uuid"},
                    "plan": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string", "format": "uuid"},
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "objective": {"type": "string", "maxLength": 10000},
                            "target_date": {"type": "string", "format": "date"},
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                    "step": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "detail": {"type": "string", "maxLength": 10000},
                            "position": {"type": "integer", "minimum": 0},
                            "due_at": {"type": "string", "format": "date-time"},
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                    "patch": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "objective": {"type": "string", "maxLength": 10000},
                            "detail": {"type": "string", "maxLength": 10000},
                            "status": {
                                "type": "string",
                                "enum": [
                                    "draft",
                                    "active",
                                    "paused",
                                    "completed",
                                    "archived",
                                    "todo",
                                    "in_progress",
                                    "skipped",
                                ],
                            },
                            "target_date": {"type": "string", "format": "date"},
                            "due_at": {"type": "string", "format": "date-time"},
                            "position": {"type": "integer", "minimum": 0},
                            "expected_version": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                },
                ["action"],
            ),
            handler=_tool_handler("manage_personal_plans"),
        )
        ctx.register_tool(
            name="manage_personal_notes",
            toolset=TOOLSET,
            schema=_schema(
                "manage_personal_notes",
                "List or upsert scoped general, task, plan, or daily notes.",
                {
                    "action": {"type": "string", "enum": ["list", "upsert"]},
                    "task_id": {"type": "string", "format": "uuid"},
                    "plan_id": {"type": "string", "format": "uuid"},
                    "note_date": {"type": "string", "format": "date"},
                    "note": {
                        "type": "object",
                        "properties": {
                            "note_id": {"type": "string", "format": "uuid"},
                            "kind": {
                                "type": "string",
                                "enum": ["general", "task", "daily"],
                            },
                            "title": {"type": "string", "minLength": 1, "maxLength": 300},
                            "content": {"type": "string", "maxLength": 100000},
                            "task_id": {"type": "string", "format": "uuid"},
                            "plan_id": {"type": "string", "format": "uuid"},
                            "note_date": {"type": "string", "format": "date"},
                            "expected_version": {"type": "integer", "minimum": 1},
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                },
                ["action"],
            ),
            handler=_tool_handler("manage_personal_notes"),
        )
        ctx.register_tool(
            name="correct_personal_memory",
            toolset=TOOLSET,
            schema=_schema(
                "correct_personal_memory",
                "Forget or replace governed memory only from an explicit natural-language "
                "user correction. Multiple matches require confirmed memory IDs.",
                {
                    "request": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "confirm_memory_ids": {
                        "type": "array",
                        "items": {"type": "string", "format": "uuid"},
                        "maxItems": 20,
                    },
                },
                ["request"],
            ),
            handler=_tool_handler("correct_personal_memory"),
        )
        ctx.register_tool(
            name="manage_personal_profile",
            toolset=TOOLSET,
            schema=_schema(
                "manage_personal_profile",
                "Read or update scoped persona preferences and set or clear a temporary "
                "style-only emotion override.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["get", "update", "set_emotion", "clear_emotion"],
                    },
                    "persona": {
                        "type": "object",
                        "properties": {
                            "user_display_name": {"type": "string", "maxLength": 100},
                            "agent_name": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 100,
                            },
                            "self_description": {"type": "string", "maxLength": 5000},
                            "communication_style": {"type": "string", "maxLength": 500},
                            "preferred_tone": {"type": "string", "maxLength": 100},
                            "locale": {"type": "string", "maxLength": 32},
                            "timezone": {"type": "string", "maxLength": 100},
                            "interests": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 50,
                            },
                            "boundaries": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 50,
                            },
                            "complete_onboarding": {"type": "boolean"},
                            "reset_onboarding": {"type": "boolean"},
                            "expected_version": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                    "emotion": {
                        "type": "object",
                        "properties": {
                            "state": {
                                "type": "string",
                                "enum": [
                                    "calm",
                                    "focused",
                                    "curious",
                                    "supportive",
                                    "celebrating",
                                    "reflective",
                                    "resting",
                                ],
                            },
                            "note": {"type": "string", "maxLength": 500},
                            "duration_minutes": {
                                "type": "integer",
                                "minimum": 5,
                                "maximum": 1440,
                            },
                        },
                        "required": ["state"],
                        "additionalProperties": False,
                    },
                },
                ["action"],
            ),
            handler=_tool_handler("manage_personal_profile"),
        )
        ctx.register_tool(
            name="manage_personal_journal",
            toolset=TOOLSET,
            schema=_schema(
                "manage_personal_journal",
                "List, read, deterministically seal, or edit scoped daily archives.",
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "seal", "update"],
                    },
                    "archive_date": {"type": "string", "format": "date"},
                    "date_from": {"type": "string", "format": "date"},
                    "date_to": {"type": "string", "format": "date"},
                    "force": {"type": "boolean"},
                    "patch": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string", "maxLength": 20000},
                            "diary": {"type": "string", "maxLength": 50000},
                            "highlights": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 50,
                            },
                            "decisions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 50,
                            },
                            "open_loops": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 50,
                            },
                            "expected_version": {"type": "integer", "minimum": 1},
                        },
                        "additionalProperties": False,
                    },
                },
                ["action"],
            ),
            handler=_tool_handler("manage_personal_journal"),
        )
    ctx.register_tool(
        name="recall_project_memory",
        toolset=TOOLSET,
        schema=_schema(
            "recall_project_memory",
            "Recall trusted project memory from HermesGraph's governed memory store.",
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["query"],
        ),
        handler=_tool_handler("recall_project_memory"),
    )
    ctx.register_tool(
        name="activate_governed_skill",
        toolset=TOOLSET,
        schema=_schema(
            "activate_governed_skill",
            "Activate the exact canary or active governed Skill pinned in this run. "
            "The result is a bounded declarative procedure and cannot grant permissions.",
            {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_]{2,63}$",
                    "maxLength": 64,
                },
                "version": {
                    "type": "string",
                    "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
                },
            },
            ["name"],
        ),
        handler=_tool_handler("activate_governed_skill"),
    )
    ctx.register_tool(
        name="hermesgraph_publish_answer",
        toolset=TOOLSET,
        schema=_schema(
            "hermesgraph_publish_answer",
            (
                "Validate and publish one final answer. Invoke exactly once, never in parallel, "
                "and stop after success. Conversational/action modes require empty claims and "
                "citation_ids with confidence=insufficient."
            ),
            {
                "answer_markdown": {"type": "string", "minLength": 1},
                "response_mode": {
                    "type": "string",
                    "enum": ["grounded", "conversational", "action"],
                },
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "evidence_ids": {
                                "type": "array",
                                "items": {"type": "string", "format": "uuid"},
                            },
                            "level": {
                                "type": "string",
                                "enum": [
                                    "verified",
                                    "supported",
                                    "inferred",
                                    "insufficient",
                                    "conflicting",
                                ],
                            },
                        },
                        "required": ["text", "evidence_ids", "level"],
                        "additionalProperties": False,
                    },
                },
                "citation_ids": {
                    "type": "array",
                    "items": {"type": "string", "format": "uuid"},
                },
                "confidence": {
                    "type": "string",
                    "enum": [
                        "verified",
                        "supported",
                        "inferred",
                        "insufficient",
                        "conflicting",
                    ],
                },
                "limitations": {"type": "array", "items": {"type": "string"}},
                "followup_queries": {"type": "array", "items": {"type": "string"}},
            },
            [
                "answer_markdown",
                "response_mode",
                "claims",
                "citation_ids",
                "confidence",
            ],
        ),
        handler=_tool_handler("hermesgraph_publish_answer"),
    )

    def snapshot_native_tool(
        tool_name: str,
        args: dict[str, Any],
        task_id: str,
        tool_call_id: str = "",
        turn_id: str = "",
        **kwargs: Any,
    ) -> dict[str, str] | None:
        del kwargs
        if tool_name not in {"memory", "skill_manage"}:
            return None
        key = _call_key(
            tool_name,
            args,
            task_id=str(task_id),
            tool_call_id=str(tool_call_id),
            turn_id=str(turn_id),
        )
        try:
            _snapshot_manager().begin(tool_name, args, key)
        except NativeSnapshotError as exc:
            return {
                "action": "block",
                "message": f"Native learning write blocked: {exc}",
            }
        return None

    def audit_native_tool(
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        task_id: str,
        session_id: str = "",
        tool_call_id: str = "",
        turn_id: str = "",
        status: str = "",
        error_type: str = "",
        **kwargs: Any,
    ) -> None:
        del kwargs
        if tool_name not in {"memory", "skill_manage", "skills_list", "skill_view", "todo"}:
            return
        snapshot = None
        snapshot_error_type = ""
        if tool_name in {"memory", "skill_manage"}:
            key = _call_key(
                tool_name,
                args,
                task_id=str(task_id),
                tool_call_id=str(tool_call_id),
                turn_id=str(turn_id),
            )
            try:
                snapshot = _snapshot_manager().finalize(key, result, status=status or None)
            except Exception as exc:
                snapshot_error_type = type(exc).__name__
        bridge_run_id = _bridge_run_id(task_id=task_id, session_id=session_id)
        if bridge_run_id is None:
            return
        encoded_task_id = parse.quote(bridge_run_id, safe="")
        _post(
            f"runs/{encoded_task_id}/events",
            {
                "tool_name": tool_name,
                "args": _sanitize_value(args),
                "result": _result_summary(result),
                "status": status or None,
                "error_type": error_type or snapshot_error_type or None,
                "applied": (
                    snapshot.get("applied")
                    if snapshot is not None
                    else _reported_applied(result, status)
                    if tool_name in {"memory", "skill_manage"}
                    else None
                ),
                "snapshot": snapshot,
            },
        )

    def signal_background_review_completion(
        session_id: str = "",
        task_id: str = "",
        completed: bool = False,
        interrupted: bool = False,
        **kwargs: Any,
    ) -> None:
        del kwargs
        bridge_run_id = _bridge_run_id(task_id=task_id, session_id=session_id)
        if bridge_run_id is None or not _is_background_review_thread():
            return
        encoded_bridge_run_id = parse.quote(bridge_run_id, safe="")
        _post(
            f"runs/{encoded_bridge_run_id}/events",
            {
                "tool_name": "hermes_background_review_completed",
                "args": {
                    "completed": bool(completed),
                    "interrupted": bool(interrupted),
                },
                "result": json.dumps(
                    {
                        "success": bool(completed and not interrupted),
                        "background_review_completed": True,
                    }
                ),
                "status": "ok" if completed and not interrupted else "incomplete",
                "applied": None,
            },
        )

    ctx.register_hook("pre_tool_call", snapshot_native_tool)
    ctx.register_hook("post_tool_call", audit_native_tool)
    ctx.register_hook("on_session_end", signal_background_review_completion)

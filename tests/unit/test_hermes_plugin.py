from pathlib import Path
from typing import Any

import yaml

from deploy.hermes import plugin


class _FixturePluginContext:
    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.hooks: dict[str, Any] = {}

    def register_tool(self, *, name: str, **kwargs: Any) -> None:
        self.tools[name] = kwargs

    def register_hook(self, name: str, handler: Any) -> None:
        self.hooks[name] = handler


def test_hermes_native_learning_reviews_every_isolated_agent_turn() -> None:
    config_path = Path(__file__).parents[2] / "deploy" / "hermes" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())

    assert config["memory"]["nudge_interval"] == 1
    assert config["skills"]["creation_nudge_interval"] == 1


def test_background_review_uses_parent_bridge_session_for_native_audit() -> None:
    assert (
        plugin._bridge_run_id(
            task_id="fc047415-4c94-459d-9f37-3162559d5d5d",
            session_id="hg_parent_run",
        )
        == "hg_parent_run"
    )
    assert (
        plugin._bridge_run_id(
            task_id="hg_foreground_run",
            session_id="",
        )
        == "hg_foreground_run"
    )
    assert plugin._bridge_run_id(task_id="standalone", session_id="local") is None


def test_background_review_completion_is_correlated_to_parent_bridge(
    monkeypatch: Any,
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(plugin, "_start_native_admin", lambda: None)
    monkeypatch.setattr(
        plugin,
        "_post",
        lambda path, payload: posted.append((path, payload)) or "{}",
    )
    monkeypatch.setattr(plugin, "_is_background_review_thread", lambda: True)
    context = _FixturePluginContext()
    plugin.register(context)

    context.hooks["on_session_end"](
        session_id="hg_parent_run",
        task_id="background-task",
        completed=True,
        interrupted=False,
    )
    monkeypatch.setattr(plugin, "_is_background_review_thread", lambda: False)
    context.hooks["on_session_end"](
        session_id="hg_parent_run",
        task_id="foreground-task",
        completed=True,
        interrupted=False,
    )

    assert len(posted) == 1
    assert posted[0][0] == "runs/hg_parent_run/events"
    assert posted[0][1]["tool_name"] == "hermes_background_review_completed"


def test_hermes_plugin_registers_bounded_graph_rag_tool_schemas(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plugin, "_start_native_admin", lambda: None)
    monkeypatch.setenv("HERMESGRAPH_GRAPH_ENABLED", "true")
    monkeypatch.setenv("HERMESGRAPH_WEB_SEARCH_ENABLED", "false")
    context = _FixturePluginContext()

    plugin.register(context)

    assert {
        "search_graph",
        "resolve_graph_entities",
        "retrieve_evidence_subgraph",
        "compare_graph_entities",
    } <= context.tools.keys()
    subgraph_schema = context.tools["retrieve_evidence_subgraph"]["schema"]["parameters"]
    assert subgraph_schema["required"] == ["query"]
    assert subgraph_schema["additionalProperties"] is False
    assert subgraph_schema["properties"]["max_hops"]["maximum"] == 3
    assert subgraph_schema["properties"]["path_limit"]["maximum"] == 100
    compare_schema = context.tools["compare_graph_entities"]["schema"]["parameters"]
    assert compare_schema["required"] == ["left_entity", "right_entity"]
    publish_schema = context.tools["hermesgraph_publish_answer"]["schema"]["parameters"]
    assert "response_mode" in publish_schema["required"]
    assert publish_schema["properties"]["response_mode"]["enum"] == [
        "grounded",
        "conversational",
        "action",
    ]
    assert "memory_ids" not in publish_schema["required"]
    assert publish_schema["properties"]["memory_ids"]["maxItems"] == 20


def test_hermes_plugin_registers_bounded_computer_workspace_tools(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plugin, "_start_native_admin", lambda: None)
    monkeypatch.setenv("HERMESGRAPH_GRAPH_ENABLED", "false")
    monkeypatch.setenv("HERMESGRAPH_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("HERMESGRAPH_COMPUTER_ENABLED", "true")
    context = _FixturePluginContext()

    plugin.register(context)

    assert {
        "list_workspace_files",
        "read_workspace_file",
        "search_workspace_files",
    } <= context.tools.keys()
    assert "activate_governed_skill" in context.tools
    read_schema = context.tools["read_workspace_file"]["schema"]["parameters"]
    assert read_schema["required"] == ["root", "path"]
    assert read_schema["additionalProperties"] is False
    assert read_schema["properties"]["max_lines"]["maximum"] == 400
    search_schema = context.tools["search_workspace_files"]["schema"]["parameters"]
    assert search_schema["properties"]["max_files"]["maximum"] == 500
    activation_schema = context.tools["activate_governed_skill"]["schema"]["parameters"]
    assert activation_schema["required"] == ["name"]


def test_hermes_plugin_registers_web_reader_and_local_general_tools(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(plugin, "_start_native_admin", lambda: None)
    monkeypatch.setenv("HERMESGRAPH_GRAPH_ENABLED", "false")
    monkeypatch.setenv("HERMESGRAPH_WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("HERMESGRAPH_GENERAL_TOOLS_ENABLED", "true")
    context = _FixturePluginContext()

    plugin.register(context)

    assert {"search_web", "read_web_page", "calculate", "current_time"} <= context.tools.keys()
    page_schema = context.tools["read_web_page"]["schema"]["parameters"]
    assert page_schema["required"] == ["url"]
    assert page_schema["properties"]["max_chars"]["maximum"] == 20_000
    calculator_schema = context.tools["calculate"]["schema"]["parameters"]
    assert calculator_schema["required"] == ["expression"]
    assert calculator_schema["additionalProperties"] is False


def test_hermes_plugin_registers_personal_control_tools(monkeypatch: Any) -> None:
    monkeypatch.setattr(plugin, "_start_native_admin", lambda: None)
    monkeypatch.setenv("HERMESGRAPH_GRAPH_ENABLED", "false")
    monkeypatch.setenv("HERMESGRAPH_WEB_SEARCH_ENABLED", "false")
    monkeypatch.setenv("HERMESGRAPH_COMPUTER_ENABLED", "false")
    monkeypatch.setenv("HERMESGRAPH_PERSONAL_ENABLED", "true")
    context = _FixturePluginContext()

    plugin.register(context)

    assert {
        "manage_personal_tasks",
        "manage_personal_plans",
        "manage_personal_notes",
        "correct_personal_memory",
        "manage_personal_profile",
        "manage_personal_journal",
    } <= context.tools.keys()
    task_schema = context.tools["manage_personal_tasks"]["schema"]["parameters"]
    assert task_schema["required"] == ["action"]
    assert task_schema["additionalProperties"] is False
    correction_schema = context.tools["correct_personal_memory"]["schema"]["parameters"]
    assert correction_schema["required"] == ["request"]
    assert correction_schema["properties"]["confirm_memory_ids"]["maxItems"] == 20
    profile_schema = context.tools["manage_personal_profile"]["schema"]["parameters"]
    assert profile_schema["properties"]["emotion"]["properties"]["duration_minutes"][
        "maximum"
    ] == 1440
    journal_schema = context.tools["manage_personal_journal"]["schema"]["parameters"]
    assert journal_schema["properties"]["patch"]["additionalProperties"] is False

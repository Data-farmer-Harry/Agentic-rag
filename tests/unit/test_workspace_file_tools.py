from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.agent.hermes_bridge import HermesCapabilityBridge, RunBudgetExceeded
from app.agent.workspace_file_tools import WorkspaceFileTools, WorkspaceToolError
from app.capabilities import AgentToolRuntime, CapabilityScopeError
from app.config import Settings
from app.domain.models import (
    RunContext,
    WorkspaceFileReadRequest,
    WorkspaceListRequest,
    WorkspaceSearchRequest,
)
from app.retrieval import InMemoryRetriever, RetrievalPipeline


def _write_docx(path: Path, text: str) -> None:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>
"""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)


def _write_xlsx(path: Path, text: str) -> None:
    shared = f"""<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>{text}</t></si>
</sst>
"""
    sheet = """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>
</worksheet>
"""
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def _workspace(root: Path, **kwargs: Any) -> WorkspaceFileTools:
    return WorkspaceFileTools({"workspace": root}, **kwargs)


@pytest.mark.asyncio
async def test_workspace_tools_list_read_search_and_extract_office_documents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "agent.py").write_text(
        "def plan():\n    return 'evidence budget'\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("API_KEY=must-not-leak", encoding="utf-8")
    (root / "api-key.txt").write_text("must-not-leak", encoding="utf-8")
    _write_docx(root / "design.docx", "Graph evidence design")
    _write_xlsx(root / "metrics.xlsx", "retrieval budget")
    tools = _workspace(root)
    context = RunContext()

    listed = await tools.list_workspace_files(
        WorkspaceListRequest(root="workspace", recursive=True),
        context,
    )
    paths = {entry.path for entry in listed.entries}
    docx = await tools.read_workspace_file(
        WorkspaceFileReadRequest(root="workspace", path="design.docx"),
        context,
    )
    xlsx = await tools.read_workspace_file(
        WorkspaceFileReadRequest(root="workspace", path="metrics.xlsx"),
        context,
    )
    search = await tools.search_workspace_files(
        WorkspaceSearchRequest(root="workspace", query="budget"),
        context,
    )

    assert {"src/agent.py", "design.docx", "metrics.xlsx"} <= paths
    assert ".env" not in paths
    assert "api-key.txt" not in paths
    assert docx.text == "Graph evidence design"
    assert xlsx.text.endswith("A1=retrieval budget")
    assert {match.path for match in search.matches} == {
        "metrics.xlsx",
        "src/agent.py",
    }
    assert len(search.evidence) == 2
    assert all(item.provenance.source_type == "workspace_file" for item in search.evidence)
    assert all(item.provenance.run_id == context.run_id for item in search.evidence)


@pytest.mark.asyncio
async def test_workspace_tools_block_escape_symlink_credentials_and_other_scopes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "visible.md").write_text("visible", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)
    tools = _workspace(root)

    with pytest.raises(WorkspaceToolError, match="stay within"):
        await tools.read_workspace_file(
            WorkspaceFileReadRequest(root="workspace", path="../outside.txt"),
            RunContext(),
        )
    with pytest.raises(WorkspaceToolError, match="symbolic links"):
        await tools.read_workspace_file(
            WorkspaceFileReadRequest(root="workspace", path="escape.txt"),
            RunContext(),
        )
    with pytest.raises(WorkspaceToolError, match="credentials"):
        await tools.read_workspace_file(
            WorkspaceFileReadRequest(root="workspace", path=".env"),
            RunContext(),
        )
    with pytest.raises(WorkspaceToolError, match="run scope"):
        await tools.read_workspace_file(
            WorkspaceFileReadRequest(root="workspace", path="visible.md"),
            RunContext(project_id="other"),
        )


@pytest.mark.asyncio
async def test_workspace_capabilities_require_scope_and_bridge_publish_citable_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "decision.md").write_text(
        "Use bounded graph traversal for relationship questions.",
        encoding="utf-8",
    )
    tools = _workspace(root)
    retrieval = RetrievalPipeline({"empty": InMemoryRetriever([])})
    runtime = AgentToolRuntime(retrieval, workspace=tools)
    context = RunContext()

    with pytest.raises(CapabilityScopeError, match="computer:read"):
        await runtime.registry.invoke(
            "read_workspace_file",
            {"root": "workspace", "path": "decision.md"},
            context=context,
        )

    bridge = HermesCapabilityBridge(
        settings=Settings(
            app_env="test",
            hermes_bridge_token="bridge-secret",
            max_computer_tool_calls=1,
        ),
        retrieval=runtime,
        workspace=runtime,
    )
    bridge_id = await bridge.open_run(context)
    result = await bridge.invoke(
        bridge_id,
        "read_workspace_file",
        {"root": "workspace", "path": "decision.md"},
    )
    evidence_id = result["result"]["evidence"][0]["evidence_id"]
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "Use bounded graph traversal.",
            "citation_ids": [evidence_id],
            "confidence": "supported",
        },
    )
    answer = await bridge.published_answer(bridge_id)

    assert answer.citations[0].provenance.source_id == "workspace:decision.md"
    assert {
        "list_workspace_files",
        "read_workspace_file",
        "search_workspace_files",
    } <= {spec.name for spec in runtime.registry.list_specs()}

    second_bridge = HermesCapabilityBridge(
        settings=Settings(
            app_env="test",
            hermes_bridge_token="bridge-secret",
            max_computer_tool_calls=1,
        ),
        retrieval=runtime,
        workspace=runtime,
    )
    second_id = await second_bridge.open_run(RunContext())
    await second_bridge.invoke(
        second_id,
        "list_workspace_files",
        {"root": "workspace"},
    )
    with pytest.raises(RunBudgetExceeded, match="computer workspace"):
        await second_bridge.invoke(
            second_id,
            "search_workspace_files",
            {"root": "workspace", "query": "bounded"},
        )

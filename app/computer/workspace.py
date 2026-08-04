from __future__ import annotations

import asyncio
import hashlib
import os
import re
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from pypdf import PdfReader

from app.domain.enums import TrustLevel
from app.domain.models import (
    EvidenceRef,
    Provenance,
    RunContext,
    WorkspaceEntry,
    WorkspaceFileReadRequest,
    WorkspaceFileReadResult,
    WorkspaceListRequest,
    WorkspaceListResult,
    WorkspaceSearchMatch,
    WorkspaceSearchRequest,
    WorkspaceSearchResult,
)

_TEXT_SUFFIXES = {
    "",
    ".c",
    ".cc",
    ".cfg",
    ".cpp",
    ".css",
    ".csv",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".markdown",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_DOCUMENT_SUFFIXES = {".docx", ".pdf", ".xlsx"}
_SUPPORTED_SUFFIXES = _TEXT_SUFFIXES | _DOCUMENT_SUFFIXES
_BLOCKED_SUFFIXES = {".der", ".key", ".p12", ".pem", ".pfx"}
_BLOCKED_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "authorized_keys",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
_BLOCKED_DIRECTORIES = {
    ".aws",
    ".git",
    ".gnupg",
    ".ssh",
    ".venv",
    "__pycache__",
    "node_modules",
    "secrets",
    "venv",
}
_SECRET_NAME = re.compile(
    r"(?:^|[._-])(?:api[_-]?key|credentials?|private[_-]?key|secrets?|tokens?)(?:[._-]|$)",
    re.IGNORECASE,
)
_DOCX_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class WorkspaceToolError(ValueError):
    pass


class ComputerWorkspaceTools:
    """Read-only, scope-bound access to explicitly mounted personal workspaces."""

    def __init__(
        self,
        roots: dict[str, Path],
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        max_file_bytes: int = 10_000_000,
        max_extracted_chars: int = 250_000,
        max_pdf_pages: int = 100,
        max_scan_entries: int = 5_000,
        max_search_files: int = 500,
        max_archive_uncompressed_bytes: int = 50_000_000,
        max_output_chars: int = 18_000,
    ) -> None:
        if not roots:
            raise ValueError("Computer workspace tools require at least one root")
        if max_file_bytes < 1_024 or max_extracted_chars < 1_000:
            raise ValueError("Computer workspace file budgets are too small")
        if max_pdf_pages < 1 or max_scan_entries < 1 or max_search_files < 1:
            raise ValueError("Computer workspace scan budgets must be positive")
        if not 1_000 <= max_output_chars <= 20_000:
            raise ValueError("Computer workspace output chars must be between 1000 and 20000")

        resolved: dict[str, Path] = {}
        for alias, configured_root in roots.items():
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", alias) is None:
                raise ValueError(f"Invalid computer workspace root alias: {alias!r}")
            root = Path(configured_root).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"Computer workspace root is not a directory: {root}")
            resolved[alias] = root

        self._roots = resolved
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._max_file_bytes = max_file_bytes
        self._max_extracted_chars = max_extracted_chars
        self._max_pdf_pages = max_pdf_pages
        self._max_scan_entries = max_scan_entries
        self._max_search_files = max_search_files
        self._max_archive_uncompressed_bytes = max_archive_uncompressed_bytes
        self._max_output_chars = max_output_chars

    async def list_workspace_files(
        self,
        request: WorkspaceListRequest,
        context: RunContext,
    ) -> WorkspaceListResult:
        self._require_scope(context)
        return await asyncio.to_thread(self._list, request)

    async def read_workspace_file(
        self,
        request: WorkspaceFileReadRequest,
        context: RunContext,
    ) -> WorkspaceFileReadResult:
        self._require_scope(context)
        return await asyncio.to_thread(self._read, request, context)

    async def search_workspace_files(
        self,
        request: WorkspaceSearchRequest,
        context: RunContext,
    ) -> WorkspaceSearchResult:
        self._require_scope(context)
        return await asyncio.to_thread(self._search, request, context)

    def _list(self, request: WorkspaceListRequest) -> WorkspaceListResult:
        root = self._root(request.root)
        target, relative = self._resolve(root, request.path)
        if not target.is_dir():
            raise WorkspaceToolError("Workspace list target is not a directory")

        iterator = target.rglob("*") if request.recursive else target.iterdir()
        entries: list[WorkspaceEntry] = []
        scanned = 0
        truncated = False
        for candidate in sorted(iterator, key=lambda item: str(item).casefold()):
            scanned += 1
            if scanned > self._max_scan_entries:
                truncated = True
                break
            item_relative = candidate.relative_to(root)
            if not self._is_safe_visible_path(item_relative) or candidate.is_symlink():
                continue
            try:
                item_stat = candidate.stat(follow_symlinks=False)
            except OSError:
                continue
            if candidate.is_dir():
                kind = "directory"
                byte_size = None
                file_format = None
            elif candidate.is_file() and self._is_supported_file(candidate):
                kind = "file"
                byte_size = item_stat.st_size
                file_format = self._format_for(candidate)
            else:
                continue
            entries.append(
                WorkspaceEntry(
                    path=item_relative.as_posix(),
                    kind=kind,
                    byte_size=byte_size,
                    modified_at=datetime.fromtimestamp(item_stat.st_mtime, tz=UTC),
                    format=file_format,
                )
            )
            if len(entries) >= request.max_entries:
                truncated = True
                break

        return WorkspaceListResult(
            root=request.root,
            path=relative.as_posix() or ".",
            entries=entries,
            scanned_entries=scanned,
            truncated=truncated,
        )

    def _read(
        self,
        request: WorkspaceFileReadRequest,
        context: RunContext,
    ) -> WorkspaceFileReadResult:
        root = self._root(request.root)
        target, relative = self._resolve(root, request.path)
        if not target.is_file() or not self._is_supported_file(target):
            raise WorkspaceToolError("Workspace file type is not supported")

        content = self._read_bounded_file(target)
        text, warnings = self._extract_text(content, target.suffix.casefold())
        lines = text.splitlines()
        selected = lines[request.start_line - 1 : request.start_line - 1 + request.max_lines]
        selected_text = "\n".join(selected)
        output_truncated = False
        if len(selected_text) > self._max_output_chars:
            selected_text = selected_text[: self._max_output_chars].rstrip()
            output_truncated = True
        end_line = request.start_line + len(selected) - 1 if selected else 0
        truncated = (
            output_truncated
            or end_line < len(lines)
            or request.start_line > max(1, len(lines))
            or len(text) >= self._max_extracted_chars
        )
        digest = hashlib.sha256(content).hexdigest()
        evidence: list[EvidenceRef] = []
        if selected_text:
            evidence.append(
                self._evidence(
                    context=context,
                    root_alias=request.root,
                    relative=relative,
                    content_hash=digest,
                    text=selected_text,
                    line_number=request.start_line,
                    line_end=end_line,
                )
            )
        return WorkspaceFileReadResult(
            root=request.root,
            path=relative.as_posix(),
            format=self._format_for(target),
            byte_size=len(content),
            content_hash=digest,
            start_line=request.start_line,
            end_line=end_line,
            total_lines=len(lines),
            text=selected_text,
            truncated=truncated,
            evidence=evidence,
            warnings=warnings,
        )

    def _search(
        self,
        request: WorkspaceSearchRequest,
        context: RunContext,
    ) -> WorkspaceSearchResult:
        if "\x00" in request.query:
            raise WorkspaceToolError("Workspace search query contains a null byte")
        root = self._root(request.root)
        target, relative = self._resolve(root, request.path)
        candidates, candidate_scan_truncated = self._candidate_files(target, root)
        file_limit = min(request.max_files, self._max_search_files)
        truncated = candidate_scan_truncated or len(candidates) > file_limit
        candidates = candidates[:file_limit]

        needle = request.query if request.case_sensitive else request.query.casefold()
        matches: list[WorkspaceSearchMatch] = []
        evidence: list[EvidenceRef] = []
        scanned_files = 0
        skipped_files = 0
        for candidate in candidates:
            scanned_files += 1
            try:
                content = self._read_bounded_file(candidate)
                text, _ = self._extract_text(content, candidate.suffix.casefold())
            except (OSError, WorkspaceToolError):
                skipped_files += 1
                continue
            digest = hashlib.sha256(content).hexdigest()
            candidate_relative = candidate.relative_to(root)
            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if request.case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                excerpt = " ".join(line.split())
                if not excerpt:
                    continue
                excerpt = excerpt[:1_000]
                item = self._evidence(
                    context=context,
                    root_alias=request.root,
                    relative=candidate_relative,
                    content_hash=digest,
                    text=excerpt,
                    line_number=line_number,
                    line_end=line_number,
                )
                evidence.append(item)
                matches.append(
                    WorkspaceSearchMatch(
                        path=candidate_relative.as_posix(),
                        line_number=line_number,
                        excerpt=excerpt,
                        evidence_id=item.evidence_id,
                    )
                )
                if len(matches) >= request.max_results:
                    truncated = True
                    break
            if len(matches) >= request.max_results:
                break

        return WorkspaceSearchResult(
            root=request.root,
            path=relative.as_posix() or ".",
            query=request.query,
            matches=matches,
            evidence=evidence,
            scanned_files=scanned_files,
            skipped_files=skipped_files,
            truncated=truncated,
        )

    def _candidate_files(self, target: Path, root: Path) -> tuple[list[Path], bool]:
        if target.is_file():
            return ([target] if self._is_supported_file(target) else []), False
        if not target.is_dir():
            raise WorkspaceToolError("Workspace search target does not exist")
        candidates: list[Path] = []
        truncated = False
        for scanned, candidate in enumerate(target.rglob("*"), start=1):
            if scanned > self._max_scan_entries or len(candidates) > self._max_search_files:
                truncated = True
                break
            relative = candidate.relative_to(root)
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or not self._is_safe_visible_path(relative)
                or not self._is_supported_file(candidate)
            ):
                continue
            candidates.append(candidate)
        return sorted(candidates, key=lambda item: str(item).casefold()), truncated

    def _resolve(self, root: Path, raw_path: str) -> tuple[Path, Path]:
        normalized = raw_path.replace("\\", "/")
        if "\x00" in normalized:
            raise WorkspaceToolError("Workspace path contains a null byte")
        relative = Path(normalized)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceToolError("Workspace path must stay within its configured root")
        if not self._is_safe_visible_path(relative):
            raise WorkspaceToolError("Workspace path is hidden or may contain credentials")

        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise WorkspaceToolError("Workspace symbolic links are not allowed")
        try:
            resolved = cursor.resolve(strict=True)
        except FileNotFoundError as exc:
            raise WorkspaceToolError("Workspace path does not exist") from exc
        if not resolved.is_relative_to(root):
            raise WorkspaceToolError("Workspace path escaped its configured root")
        return resolved, relative

    def _root(self, alias: str) -> Path:
        try:
            return self._roots[alias]
        except KeyError as exc:
            raise WorkspaceToolError(f"Unknown computer workspace root: {alias}") from exc

    def _require_scope(self, context: RunContext) -> None:
        if context.tenant_id != self._tenant_id or context.project_id != self._project_id:
            raise WorkspaceToolError("Computer workspace is not granted to this run scope")

    def _read_bounded_file(self, path: Path) -> bytes:
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise WorkspaceToolError("Unable to open workspace file safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size > self._max_file_bytes:
                raise WorkspaceToolError("Workspace file exceeds the configured byte budget")
            content = bytearray()
            while len(content) <= self._max_file_bytes:
                chunk = os.read(descriptor, min(1_048_576, self._max_file_bytes + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > self._max_file_bytes:
                raise WorkspaceToolError("Workspace file exceeds the configured byte budget")
            return bytes(content)
        finally:
            os.close(descriptor)

    def _extract_text(self, content: bytes, suffix: str) -> tuple[str, list[str]]:
        warnings: list[str] = []
        try:
            if suffix == ".pdf":
                text, warnings = self._extract_pdf(content)
            elif suffix == ".docx":
                text = self._extract_docx(content)
            elif suffix == ".xlsx":
                text, warnings = self._extract_xlsx(content)
            else:
                if b"\x00" in content[:8_192]:
                    raise WorkspaceToolError("Workspace file appears to be binary")
                text = content.decode("utf-8", errors="replace")
        except (BadZipFile, ElementTree.ParseError) as exc:
            raise WorkspaceToolError("Workspace document is malformed") from exc
        text = self._sanitize_text(text)
        if len(text) > self._max_extracted_chars:
            text = text[: self._max_extracted_chars]
            warnings.append("extracted_text_truncated")
        return text, warnings

    def _extract_pdf(self, content: bytes) -> tuple[str, list[str]]:
        try:
            reader = PdfReader(BytesIO(content), strict=False)
        except Exception as exc:
            raise WorkspaceToolError("Workspace PDF could not be parsed") from exc
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as exc:
                raise WorkspaceToolError("Encrypted workspace PDFs are not supported") from exc
            if not unlocked:
                raise WorkspaceToolError("Encrypted workspace PDFs are not supported")
        warnings: list[str] = []
        if len(reader.pages) > self._max_pdf_pages:
            warnings.append("pdf_page_limit_reached")
        parts: list[str] = []
        for page_number, page in enumerate(reader.pages[: self._max_pdf_pages], start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                warnings.append(f"pdf_page_{page_number}_unreadable")
                continue
            if page_text.strip():
                parts.append(f"--- Page {page_number} ---\n{page_text}")
        return "\n\n".join(parts), warnings

    def _extract_docx(self, content: bytes) -> str:
        with ZipFile(BytesIO(content)) as archive:
            self._validate_archive(archive)
            try:
                document = archive.read("word/document.xml")
            except KeyError as exc:
                raise WorkspaceToolError("DOCX document.xml is missing") from exc
        root = ElementTree.fromstring(document)
        paragraphs: list[str] = []
        paragraph_tag = f"{{{_DOCX_WORD_NS}}}p"
        text_tag = f"{{{_DOCX_WORD_NS}}}t"
        tab_tag = f"{{{_DOCX_WORD_NS}}}tab"
        break_tag = f"{{{_DOCX_WORD_NS}}}br"
        for paragraph in root.iter(paragraph_tag):
            parts: list[str] = []
            for element in paragraph.iter():
                if element.tag == text_tag and element.text:
                    parts.append(element.text)
                elif element.tag == tab_tag:
                    parts.append("\t")
                elif element.tag == break_tag:
                    parts.append("\n")
            value = "".join(parts).strip()
            if value:
                paragraphs.append(value)
        return "\n".join(paragraphs)

    def _extract_xlsx(self, content: bytes) -> tuple[str, list[str]]:
        warnings: list[str] = []
        with ZipFile(BytesIO(content)) as archive:
            self._validate_archive(archive)
            shared_strings = self._xlsx_shared_strings(archive)
            sheet_names = sorted(
                name
                for name in archive.namelist()
                if re.fullmatch(r"xl/worksheets/sheet[0-9]+\.xml", name)
            )
            if len(sheet_names) > 20:
                sheet_names = sheet_names[:20]
                warnings.append("xlsx_sheet_limit_reached")
            parts: list[str] = []
            for sheet_name in sheet_names:
                sheet = ElementTree.fromstring(archive.read(sheet_name))
                parts.append(f"--- {Path(sheet_name).stem} ---")
                row_count = 0
                for row in sheet.iter(f"{{{_XLSX_MAIN_NS}}}row"):
                    row_count += 1
                    if row_count > 5_000:
                        warnings.append(f"{Path(sheet_name).stem}_row_limit_reached")
                        break
                    values: list[str] = []
                    for cell in row.findall(f"{{{_XLSX_MAIN_NS}}}c"):
                        reference = cell.attrib.get("r", "")
                        value = self._xlsx_cell_value(cell, shared_strings)
                        if value:
                            values.append(f"{reference}={value}" if reference else value)
                    if values:
                        parts.append("\t".join(values))
        return "\n".join(parts), warnings

    def _xlsx_shared_strings(self, archive: ZipFile) -> list[str]:
        try:
            payload = archive.read("xl/sharedStrings.xml")
        except KeyError:
            return []
        root = ElementTree.fromstring(payload)
        values: list[str] = []
        for item in root.findall(f"{{{_XLSX_MAIN_NS}}}si"):
            values.append(
                "".join(
                    element.text or ""
                    for element in item.iter(f"{{{_XLSX_MAIN_NS}}}t")
                )
            )
        return values

    @staticmethod
    def _xlsx_cell_value(cell: Any, shared_strings: list[str]) -> str:
        inline = cell.find(f"{{{_XLSX_MAIN_NS}}}is")
        if inline is not None:
            return "".join(
                element.text or ""
                for element in inline.iter(f"{{{_XLSX_MAIN_NS}}}t")
            ).strip()
        value_node = cell.find(f"{{{_XLSX_MAIN_NS}}}v")
        if value_node is None or value_node.text is None:
            return ""
        value = str(value_node.text)
        if cell.attrib.get("t") == "s":
            try:
                return shared_strings[int(value)].strip()
            except (IndexError, ValueError):
                return ""
        return value.strip()

    def _validate_archive(self, archive: ZipFile) -> None:
        members = archive.infolist()
        if len(members) > 10_000:
            raise WorkspaceToolError("Workspace archive contains too many members")
        if sum(member.file_size for member in members) > self._max_archive_uncompressed_bytes:
            raise WorkspaceToolError("Workspace archive exceeds its extraction budget")
        for member in members:
            normalized = member.filename.replace("\\", "/")
            if normalized.startswith("/") or ".." in Path(normalized).parts:
                raise WorkspaceToolError("Workspace archive contains an unsafe member path")

    @staticmethod
    def _sanitize_text(value: str) -> str:
        return "".join(
            character
            for character in value
            if ord(character) >= 32 or character in {"\n", "\r", "\t"}
        ).replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _is_safe_visible_path(relative: Path) -> bool:
        for part in relative.parts:
            normalized = part.casefold()
            if (
                not part
                or part in {".", ".."}
                or normalized.startswith(".")
                or normalized in _BLOCKED_DIRECTORIES
                or normalized in _BLOCKED_NAMES
                or Path(part).suffix.casefold() in _BLOCKED_SUFFIXES
                or _SECRET_NAME.search(normalized) is not None
            ):
                return False
        return True

    @staticmethod
    def _is_supported_file(path: Path) -> bool:
        return (
            path.suffix.casefold() in _SUPPORTED_SUFFIXES
            and path.suffix.casefold() not in _BLOCKED_SUFFIXES
        )

    @staticmethod
    def _format_for(path: Path) -> str:
        suffix = path.suffix.casefold().lstrip(".")
        return suffix or "text"

    @staticmethod
    def _evidence(
        *,
        context: RunContext,
        root_alias: str,
        relative: Path,
        content_hash: str,
        text: str,
        line_number: int,
        line_end: int,
    ) -> EvidenceRef:
        return EvidenceRef(
            text=text,
            title=relative.as_posix(),
            provenance=Provenance(
                source_type="workspace_file",
                source_id=f"{root_alias}:{relative.as_posix()}",
                run_id=context.run_id,
                content_hash=content_hash,
                locator={
                    "root": root_alias,
                    "path": relative.as_posix(),
                    "line_start": line_number,
                    "line_end": line_end,
                },
                trust=TrustLevel.USER_ASSERTED,
            ),
            metadata={
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "workspace_root": root_alias,
            },
        )

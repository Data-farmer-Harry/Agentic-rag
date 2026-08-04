from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


class NativeSnapshotError(RuntimeError):
    pass


class NativeSnapshotConflict(NativeSnapshotError):
    pass


class NativeSnapshotNotFound(NativeSnapshotError):
    pass


@dataclass(frozen=True, slots=True)
class NativeTarget:
    artifact_kind: str
    storage_kind: str
    target_id: str
    path: Path
    action: str


@dataclass(slots=True)
class _PendingSnapshot:
    call_key: str
    snapshot_id: str
    snapshot_dir: Path
    target: NativeTarget
    target_key: str
    before_exists: bool
    before_hash: str | None
    created_at: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _hash_path(path: Path, storage_kind: str, *, max_bytes: int) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise NativeSnapshotError("Native learning target may not be a symlink")

    digest = hashlib.sha256()
    consumed = 0
    if storage_kind == "file":
        if not path.is_file():
            raise NativeSnapshotError("Native learning target changed storage type")
        consumed = path.stat().st_size
        if consumed > max_bytes:
            raise NativeSnapshotError("Native learning target exceeds snapshot limit")
        digest.update(b"file\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    if not path.is_dir():
        raise NativeSnapshotError("Native learning target changed storage type")
    digest.update(b"directory\0")
    for candidate in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise NativeSnapshotError("Native learning trees may not contain symlinks")
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        if candidate.is_dir():
            digest.update(b"D\0" + relative + b"\0")
            continue
        if not candidate.is_file():
            raise NativeSnapshotError("Native learning trees may contain regular files only")
        consumed += candidate.stat().st_size
        if consumed > max_bytes:
            raise NativeSnapshotError("Native learning target exceeds snapshot limit")
        digest.update(b"F\0" + relative + b"\0")
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def resolve_native_target(home: Path, tool_name: str, args: dict[str, Any]) -> NativeTarget:
    action = str(args.get("action") or "").strip()
    if tool_name == "memory":
        if action not in {"add", "replace", "remove", "batch"}:
            raise NativeSnapshotError("Unsupported Hermes memory mutation")
        target = str(args.get("target") or "memory")
        if target not in {"memory", "user"}:
            raise NativeSnapshotError("Unsupported Hermes memory target")
        filename = "USER.md" if target == "user" else "MEMORY.md"
        return NativeTarget(
            artifact_kind="memory",
            storage_kind="file",
            target_id=target,
            path=home / "memories" / filename,
            action=action,
        )

    if tool_name != "skill_manage":
        raise NativeSnapshotError("Tool does not mutate Hermes native learning")
    if action not in {"create", "edit", "patch", "delete", "write_file", "remove_file"}:
        raise NativeSnapshotError("Unsupported Hermes skill mutation")
    name = str(args.get("name") or "").strip()
    if not name:
        raise NativeSnapshotError("Hermes skill name is required")

    path: Path | None = None
    if action != "create":
        try:
            from tools.skill_manager_tool import _find_skill

            match = _find_skill(name)
            if match is not None:
                path = Path(match["path"])
        except Exception:
            path = None
    if path is None:
        category = str(args.get("category") or "").strip()
        path = home / "skills" / category / name if category else home / "skills" / name
    return NativeTarget(
        artifact_kind="skill",
        storage_kind="directory",
        target_id=name,
        path=path,
        action=action,
    )


class NativeSnapshotManager:
    def __init__(
        self,
        *,
        home: Path | str,
        max_bytes: int = 5_000_000,
        max_total_bytes: int = 1_000_000_000,
        terminal_retention_days: int = 7,
        no_change_retention_hours: int = 24,
        resolver: Callable[[Path, str, dict[str, Any]], NativeTarget] = resolve_native_target,
    ) -> None:
        if max_bytes < 1_024:
            raise NativeSnapshotError("Native snapshot limit must be at least 1024 bytes")
        if max_total_bytes < 1_024:
            raise NativeSnapshotError("Native snapshot capacity must be at least 1024 bytes")
        if not 1 <= terminal_retention_days <= 3_650:
            raise NativeSnapshotError(
                "Terminal snapshot retention must be between 1 and 3650 days"
            )
        if not 1 <= no_change_retention_hours <= 87_600:
            raise NativeSnapshotError(
                "No-change snapshot retention must be between 1 and 87600 hours"
            )
        self._home = Path(home).expanduser().resolve()
        self._snapshot_root = self._home / ".hermesgraph" / "native_snapshots"
        self._max_bytes = max_bytes
        self._max_total_bytes = max_total_bytes
        self._terminal_retention_days = terminal_retention_days
        self._no_change_retention_hours = no_change_retention_hours
        self._resolver = resolver
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingSnapshot] = {}
        self._active_targets: dict[str, str] = {}
        self._rollback_targets: set[str] = set()

    @property
    def home(self) -> Path:
        return self._home

    def begin(self, tool_name: str, args: dict[str, Any], call_key: str) -> dict[str, Any]:
        self.collect_garbage()
        if self.usage()["total_bytes"] >= self._max_total_bytes:
            raise NativeSnapshotError("Native snapshot storage capacity is exhausted")
        target = self._resolver(self._home, tool_name, args)
        target_path = self._confined_target(target.path)
        target = NativeTarget(
            artifact_kind=target.artifact_kind,
            storage_kind=target.storage_kind,
            target_id=target.target_id,
            path=target_path,
            action=target.action,
        )
        target_key = target_path.relative_to(self._home).as_posix()

        with self._lock:
            if call_key in self._pending:
                raise NativeSnapshotConflict("Native mutation call is already active")
            if target_key in self._active_targets or target_key in self._rollback_targets:
                raise NativeSnapshotConflict("Native learning target is busy")
            self._active_targets[target_key] = call_key

        snapshot_id = uuid4().hex
        snapshot_dir = self._snapshot_root / snapshot_id
        created_at = _utc_now()
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=False)
            before_exists = target_path.exists()
            before_hash = _hash_path(
                target_path,
                target.storage_kind,
                max_bytes=self._max_bytes,
            )
            if before_exists:
                before_path = snapshot_dir / "before"
                if target.storage_kind == "file":
                    shutil.copy2(target_path, before_path)
                else:
                    shutil.copytree(target_path, before_path, symlinks=False)
                copied_hash = _hash_path(
                    before_path,
                    target.storage_kind,
                    max_bytes=self._max_bytes,
                )
                if copied_hash != before_hash:
                    raise NativeSnapshotConflict("Native learning target changed during snapshot")
            elif target_path.exists():
                raise NativeSnapshotConflict("Native learning target appeared during snapshot")

            pending = _PendingSnapshot(
                call_key=call_key,
                snapshot_id=snapshot_id,
                snapshot_dir=snapshot_dir,
                target=target,
                target_key=target_key,
                before_exists=before_exists,
                before_hash=before_hash,
                created_at=created_at,
            )
            _atomic_json(snapshot_dir / "manifest.json", self._manifest(pending, state="pending"))
            if self.usage()["total_bytes"] > self._max_total_bytes:
                raise NativeSnapshotError("Native snapshot storage capacity would be exceeded")
            with self._lock:
                self._pending[call_key] = pending
            return {
                "snapshot_id": snapshot_id,
                "target_kind": target.artifact_kind,
                "target_id": target.target_id,
                "before_hash": before_hash,
            }
        except Exception:
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            with self._lock:
                self._active_targets.pop(target_key, None)
            raise

    def finalize(
        self,
        call_key: str,
        result: Any,
        *,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            pending = self._pending.pop(call_key, None)
        if pending is None:
            return None

        success, reported_applied = _native_result_state(result, status=status)
        try:
            if not success or not reported_applied:
                shutil.rmtree(pending.snapshot_dir, ignore_errors=True)
                return {
                    "snapshot_id": pending.snapshot_id,
                    "target_kind": pending.target.artifact_kind,
                    "target_id": pending.target.target_id,
                    "before_hash": pending.before_hash,
                    "after_hash": pending.before_hash,
                    "applied": False,
                    "rollback_supported": False,
                    "reason": "tool_failed" if not success else "write_staged",
                }

            try:
                after_hash = _hash_path(
                    pending.target.path,
                    pending.target.storage_kind,
                    max_bytes=self._max_bytes,
                )
            except Exception as exc:
                manifest = self._manifest(
                    pending,
                    state="after_hash_failed",
                    rollback_supported=False,
                    reason=type(exc).__name__,
                )
                _atomic_json(pending.snapshot_dir / "manifest.json", manifest)
                return self._public_metadata(manifest, applied=True)

            applied = after_hash != pending.before_hash
            rollback_supported = applied
            reason = None if applied else "no_content_change"
            manifest = self._manifest(
                pending,
                state="ready" if applied else "no_change",
                after_hash=after_hash,
                rollback_supported=rollback_supported,
                reason=reason,
            )
            _atomic_json(pending.snapshot_dir / "manifest.json", manifest)
            return self._public_metadata(manifest, applied=applied)
        finally:
            with self._lock:
                self._active_targets.pop(pending.target_key, None)

    def rollback(self, snapshot_id: str, *, expected_after_hash: str) -> dict[str, Any]:
        normalized_id = self._normalize_snapshot_id(snapshot_id)
        snapshot_dir = self._snapshot_root / normalized_id
        manifest_path = snapshot_dir / "manifest.json"
        with self._lock:
            if not manifest_path.is_file():
                raise NativeSnapshotNotFound("Native learning snapshot was not found")
            manifest = self._read_manifest(manifest_path)
            target_path = self._confined_target(
                self._home / str(manifest["target_relpath"])
            )
            target_key = target_path.relative_to(self._home).as_posix()
            storage_kind = str(manifest["storage_kind"])
            after_hash = manifest.get("after_hash")
            before_hash = manifest.get("before_hash")
            if not isinstance(after_hash, str) or not hmac.compare_digest(
                expected_after_hash,
                after_hash,
            ):
                raise NativeSnapshotConflict(
                    "Rollback precondition does not match snapshot"
                )
            if target_key in self._active_targets or target_key in self._rollback_targets:
                raise NativeSnapshotConflict("Native learning target is busy")
            self._rollback_targets.add(target_key)

        try:
            current_hash = _hash_path(target_path, storage_kind, max_bytes=self._max_bytes)
            if manifest.get("state") == "rolled_back":
                if current_hash != before_hash:
                    raise NativeSnapshotConflict("Rolled-back target has changed again")
                return {
                    "success": True,
                    "snapshot_id": normalized_id,
                    "state": "rolled_back",
                    "current_hash": current_hash,
                    "idempotent": True,
                }
            if manifest.get("state") != "ready" or not manifest.get("rollback_supported"):
                raise NativeSnapshotConflict("Snapshot is not available for rollback")
            if current_hash != after_hash:
                raise NativeSnapshotConflict(
                    "Native learning target changed after the audited write"
                )

            self._restore(snapshot_dir, target_path, manifest)
            restored_hash = _hash_path(target_path, storage_kind, max_bytes=self._max_bytes)
            if restored_hash != before_hash:
                raise NativeSnapshotError("Native learning rollback verification failed")
            manifest["state"] = "rolled_back"
            manifest["rolled_back_at"] = _utc_now()
            manifest["purge_after"] = (
                datetime.now(UTC) + timedelta(days=self._terminal_retention_days)
            ).isoformat()
            _atomic_json(manifest_path, manifest)
            self._clear_skill_prompt_cache(str(manifest.get("target_kind")))
            return {
                "success": True,
                "snapshot_id": normalized_id,
                "state": "rolled_back",
                "current_hash": restored_hash,
                "idempotent": False,
            }
        finally:
            with self._lock:
                self._rollback_targets.discard(target_key)

    def mark_accepted(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
        retention_days: int,
    ) -> dict[str, Any]:
        if not 1 <= retention_days <= 3_650:
            raise NativeSnapshotError("Snapshot retention must be between 1 and 3650 days")
        normalized_id = self._normalize_snapshot_id(snapshot_id)
        manifest_path = self._snapshot_root / normalized_id / "manifest.json"
        with self._lock:
            if not manifest_path.is_file():
                raise NativeSnapshotNotFound("Native learning snapshot was not found")
            manifest = self._read_manifest(manifest_path)
            target_key = str(manifest.get("target_relpath") or "")
            if target_key in self._active_targets or target_key in self._rollback_targets:
                raise NativeSnapshotConflict("Native learning target is busy")
            after_hash = manifest.get("after_hash")
            if not isinstance(after_hash, str) or not hmac.compare_digest(
                expected_after_hash,
                after_hash,
            ):
                raise NativeSnapshotConflict("Review precondition does not match snapshot")
            if manifest.get("state") != "ready" or not manifest.get("rollback_supported"):
                raise NativeSnapshotConflict("Snapshot is not reviewable")
            if manifest.get("review_state") == "accepted" and manifest.get("purge_after"):
                return {
                    "success": True,
                    "snapshot_id": normalized_id,
                    "review_state": "accepted",
                    "retention_until": manifest["purge_after"],
                    "idempotent": True,
                }
            reviewed_at = datetime.now(UTC)
            retention_until = reviewed_at + timedelta(days=retention_days)
            manifest["review_state"] = "accepted"
            manifest["reviewed_at"] = reviewed_at.isoformat()
            manifest["purge_after"] = retention_until.isoformat()
            _atomic_json(manifest_path, manifest)
            return {
                "success": True,
                "snapshot_id": normalized_id,
                "review_state": "accepted",
                "retention_until": retention_until.isoformat(),
                "idempotent": False,
            }

    def collect_garbage(
        self,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        effective_now = now or datetime.now(UTC)
        reclaimed_bytes = 0
        eligible = 0
        deleted = 0
        examined = 0
        with self._lock:
            if not self._snapshot_root.exists():
                return {
                    "dry_run": dry_run,
                    "examined": 0,
                    "eligible": 0,
                    "deleted": 0,
                    "reclaimed_bytes": 0,
                }
            pending_ids = {item.snapshot_id for item in self._pending.values()}
            busy_targets = set(self._active_targets) | self._rollback_targets
            for snapshot_dir in sorted(self._snapshot_root.iterdir()):
                if not snapshot_dir.is_dir() or snapshot_dir.name in pending_ids:
                    continue
                manifest_path = snapshot_dir / "manifest.json"
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = self._read_manifest(manifest_path)
                except NativeSnapshotError:
                    continue
                examined += 1
                if str(manifest.get("target_relpath") or "") in busy_targets:
                    continue
                if not self._gc_eligible(manifest, now=effective_now):
                    continue
                eligible += 1
                size = self._directory_size(snapshot_dir)
                reclaimed_bytes += size
                if not dry_run:
                    try:
                        shutil.rmtree(snapshot_dir)
                    except FileNotFoundError:
                        pass
                    else:
                        deleted += 1
        return {
            "dry_run": dry_run,
            "examined": examined,
            "eligible": eligible,
            "deleted": deleted,
            "reclaimed_bytes": reclaimed_bytes,
        }

    def usage(self, *, now: datetime | None = None) -> dict[str, Any]:
        total_bytes = 0
        snapshot_count = 0
        state_counts: dict[str, int] = {}
        gc_eligible = 0
        effective_now = now or datetime.now(UTC)
        with self._lock:
            if self._snapshot_root.exists():
                for snapshot_dir in self._snapshot_root.iterdir():
                    if not snapshot_dir.is_dir():
                        continue
                    snapshot_count += 1
                    total_bytes += self._directory_size(snapshot_dir)
                    manifest_path = snapshot_dir / "manifest.json"
                    if not manifest_path.is_file():
                        state = "missing_manifest"
                    else:
                        try:
                            manifest = self._read_manifest(manifest_path)
                            state = str(manifest.get("state") or "unknown")
                            if self._gc_eligible(manifest, now=effective_now):
                                gc_eligible += 1
                        except NativeSnapshotError:
                            state = "invalid_manifest"
                    state_counts[state] = state_counts.get(state, 0) + 1
        utilization = total_bytes / self._max_total_bytes if self._max_total_bytes else 1.0
        capacity_status = (
            "critical" if utilization >= 1.0 else "warning" if utilization >= 0.8 else "ok"
        )
        return {
            "snapshot_count": snapshot_count,
            "total_bytes": total_bytes,
            "max_total_bytes": self._max_total_bytes,
            "utilization": round(utilization, 6),
            "capacity_status": capacity_status,
            "state_counts": state_counts,
            "gc_eligible": gc_eligible,
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "service": "hermesgraph-native-learning-admin",
                "pending_mutations": len(self._pending),
                "active_rollbacks": len(self._rollback_targets),
                "storage": self.usage(),
                "backup": {
                    "relative_path": ".hermesgraph/native_snapshots",
                    "included_in_hermes_full_backup": True,
                },
            }

    def _confined_target(self, path: Path) -> Path:
        raw = path.expanduser()
        cursor = raw
        while cursor != self._home and _is_relative_to(cursor, self._home):
            if cursor.exists() and cursor.is_symlink():
                raise NativeSnapshotError("Native learning path may not contain symlinks")
            cursor = cursor.parent
        resolved = raw.resolve(strict=False)
        if not _is_relative_to(resolved, self._home) or resolved == self._home:
            raise NativeSnapshotError("Native learning path escapes HERMES_HOME")
        return resolved

    def _manifest(
        self,
        pending: _PendingSnapshot,
        *,
        state: str,
        after_hash: str | None = None,
        rollback_supported: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "snapshot_id": pending.snapshot_id,
            "state": state,
            "target_kind": pending.target.artifact_kind,
            "storage_kind": pending.target.storage_kind,
            "target_id": pending.target.target_id,
            "target_relpath": pending.target.path.relative_to(self._home).as_posix(),
            "action": pending.target.action,
            "before_exists": pending.before_exists,
            "before_hash": pending.before_hash,
            "after_hash": after_hash,
            "rollback_supported": rollback_supported,
            "reason": reason,
            "created_at": pending.created_at,
        }

    @staticmethod
    def _public_metadata(manifest: dict[str, Any], *, applied: bool) -> dict[str, Any]:
        return {
            "snapshot_id": manifest["snapshot_id"],
            "target_kind": manifest["target_kind"],
            "target_id": manifest["target_id"],
            "before_hash": manifest.get("before_hash"),
            "after_hash": manifest.get("after_hash"),
            "applied": applied,
            "rollback_supported": bool(manifest.get("rollback_supported")),
            "reason": manifest.get("reason"),
        }

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NativeSnapshotError("Native learning snapshot manifest is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise NativeSnapshotError("Native learning snapshot schema is unsupported")
        return payload

    @staticmethod
    def _normalize_snapshot_id(snapshot_id: str) -> str:
        try:
            return UUID(snapshot_id).hex
        except ValueError as exc:
            raise NativeSnapshotNotFound("Native learning snapshot was not found") from exc

    @staticmethod
    def _clear_skill_prompt_cache(target_kind: str) -> None:
        if target_kind != "skill":
            return
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache

            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass

    def _gc_eligible(self, manifest: dict[str, Any], *, now: datetime) -> bool:
        state = manifest.get("state")
        if state == "no_change":
            created_at = _parse_time(manifest.get("created_at"))
            return created_at is not None and now >= created_at + timedelta(
                hours=self._no_change_retention_hours
            )
        if state == "rolled_back":
            purge_after = _parse_time(manifest.get("purge_after"))
            if purge_after is None:
                rolled_back_at = _parse_time(manifest.get("rolled_back_at"))
                purge_after = (
                    rolled_back_at + timedelta(days=self._terminal_retention_days)
                    if rolled_back_at is not None
                    else None
                )
            return purge_after is not None and now >= purge_after
        if state == "ready" and manifest.get("review_state") == "accepted":
            purge_after = _parse_time(manifest.get("purge_after"))
            return purge_after is not None and now >= purge_after
        return False

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        try:
            candidates = path.rglob("*")
            for candidate in candidates:
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        total += candidate.stat().st_size
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            return total
        return total

    @staticmethod
    def _restore(snapshot_dir: Path, target: Path, manifest: dict[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.get("before_exists"):
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            return

        source = snapshot_dir / "before"
        storage_kind = str(manifest["storage_kind"])
        restore_path = target.parent / f".{target.name}.hg-restore-{uuid4().hex}"
        displaced_path = target.parent / f".{target.name}.hg-current-{uuid4().hex}"
        displaced = False
        try:
            if storage_kind == "file":
                shutil.copy2(source, restore_path)
            else:
                shutil.copytree(source, restore_path, symlinks=False)
            if target.exists():
                os.replace(target, displaced_path)
                displaced = True
            os.replace(restore_path, target)
            if displaced_path.is_dir():
                shutil.rmtree(displaced_path)
            elif displaced_path.exists():
                displaced_path.unlink()
        except Exception:
            if displaced and not target.exists() and displaced_path.exists():
                os.replace(displaced_path, target)
            raise
        finally:
            if restore_path.is_dir():
                shutil.rmtree(restore_path, ignore_errors=True)
            elif restore_path.exists():
                restore_path.unlink(missing_ok=True)


def _native_result_state(result: Any, *, status: str | None) -> tuple[bool, bool]:
    if status not in {None, "", "ok"}:
        return False, False
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = None
    if isinstance(parsed, dict):
        success = parsed.get("success") is not False and not parsed.get("error")
        staged = bool(parsed.get("staged") or parsed.get("pending_id"))
        return success, success and not staged
    return True, True


def start_admin_server(
    manager: NativeSnapshotManager,
    *,
    host: str,
    port: int,
    token: str,
) -> ThreadingHTTPServer:
    if not token:
        raise NativeSnapshotError("HERMESGRAPH_NATIVE_ADMIN_TOKEN is required")

    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesGraphNativeAdmin/1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                self._respond(401, {"error": "invalid_credentials"})
                return
            if self.path != "/health":
                self._respond(404, {"error": "not_found"})
                return
            self._respond(200, manager.health())

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._respond(401, {"error": "invalid_credentials"})
                return
            prefix = "/v1/native-snapshots/"
            operation = ""
            snapshot_id = ""
            if self.path == f"{prefix}gc":
                operation = "gc"
            elif self.path.startswith(prefix) and self.path.endswith("/accepted"):
                operation = "accepted"
                snapshot_id = self.path[len(prefix) : -len("/accepted")]
            elif self.path.startswith(prefix) and self.path.endswith("/rollback"):
                operation = "rollback"
                snapshot_id = self.path[len(prefix) : -len("/rollback")]
            else:
                self._respond(404, {"error": "not_found"})
                return
            try:
                payload = self._read_payload()
                if operation == "gc":
                    dry_run = payload.get("dry_run", False)
                    if not isinstance(dry_run, bool):
                        raise ValueError("dry_run must be a boolean")
                    response = manager.collect_garbage(dry_run=dry_run)
                else:
                    expected = payload.get("expected_after_hash")
                    if not isinstance(expected, str) or len(expected) != 64:
                        raise ValueError("expected_after_hash is required")
                    if operation == "accepted":
                        retention_days = payload.get("retention_days")
                        if not isinstance(retention_days, int) or isinstance(
                            retention_days, bool
                        ):
                            raise ValueError("retention_days must be an integer")
                        response = manager.mark_accepted(
                            snapshot_id,
                            expected_after_hash=expected,
                            retention_days=retention_days,
                        )
                    else:
                        response = manager.rollback(
                            snapshot_id,
                            expected_after_hash=expected,
                        )
            except NativeSnapshotNotFound as exc:
                self._respond(404, {"error": str(exc)})
                return
            except NativeSnapshotConflict as exc:
                self._respond(409, {"error": str(exc)})
                return
            except (ValueError, json.JSONDecodeError) as exc:
                self._respond(400, {"error": str(exc)})
                return
            except NativeSnapshotError as exc:
                self._respond(400 if operation == "accepted" else 500, {"error": str(exc)})
                return
            self._respond(200, response)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _authorized(self) -> bool:
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                return False
            supplied = authorization[len("Bearer ") :].strip()
            return bool(supplied) and hmac.compare_digest(supplied, token)

        def _read_payload(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 16_384:
                raise ValueError("invalid body size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            return payload

        def _respond(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(
        target=server.serve_forever,
        name="hermesgraph-native-admin",
        daemon=True,
    )
    thread.start()
    return server

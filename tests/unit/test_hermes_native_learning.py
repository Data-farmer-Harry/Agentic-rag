from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.agent.hermes_native_learning import (
    HermesNativeAcceptanceResult,
    HermesNativeAdminClient,
    HermesNativeAdminHealth,
    HermesNativeLearningConflict,
    HermesNativeLearningService,
    HermesNativeRollbackResult,
)
from app.config import Settings
from app.domain.models import LearningChangeSet
from app.learning.change_set import JsonLearningChangeSetRepository
from deploy.hermes.plugin.native_snapshots import (
    NativeSnapshotConflict,
    NativeSnapshotError,
    NativeSnapshotManager,
    NativeTarget,
    start_admin_server,
)


def _manager(
    home: Path,
    target: Path,
    storage_kind: str,
    **options: Any,
) -> NativeSnapshotManager:
    def resolver(
        resolved_home: Path,
        tool_name: str,
        args: dict[str, Any],
    ) -> NativeTarget:
        del resolved_home, tool_name, args
        return NativeTarget(
            artifact_kind="memory" if storage_kind == "file" else "skill",
            storage_kind=storage_kind,
            target_id=target.name,
            path=target,
            action="edit",
        )

    return NativeSnapshotManager(
        home=home,
        max_bytes=100_000,
        resolver=resolver,
        **options,
    )


def test_native_snapshot_rolls_back_exact_file_and_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    manager = _manager(home, target, "file")

    pending = manager.begin("memory", {"action": "replace"}, "call-1")
    target.write_text("after", encoding="utf-8")
    snapshot = manager.finalize("call-1", '{"success": true}', status="ok")

    assert snapshot is not None
    assert snapshot["applied"] is True
    assert snapshot["rollback_supported"] is True
    result = manager.rollback(
        pending["snapshot_id"],
        expected_after_hash=snapshot["after_hash"],
    )
    repeated = manager.rollback(
        pending["snapshot_id"],
        expected_after_hash=snapshot["after_hash"],
    )

    assert target.read_text(encoding="utf-8") == "before"
    assert result["idempotent"] is False
    assert repeated["idempotent"] is True


def test_native_snapshot_refuses_rollback_after_state_drift(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "skills" / "fixture"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("before", encoding="utf-8")
    manager = _manager(home, target, "directory")

    pending = manager.begin("skill_manage", {"action": "edit"}, "call-1")
    (target / "SKILL.md").write_text("after", encoding="utf-8")
    snapshot = manager.finalize("call-1", '{"success": true}', status="ok")
    assert snapshot is not None
    (target / "SKILL.md").write_text("newer knowledge", encoding="utf-8")

    with pytest.raises(NativeSnapshotConflict, match="changed after"):
        manager.rollback(
            pending["snapshot_id"],
            expected_after_hash=snapshot["after_hash"],
        )

    assert (target / "SKILL.md").read_text(encoding="utf-8") == "newer knowledge"


def test_native_snapshot_removes_new_skill_on_rollback(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "skills" / "new-skill"
    manager = _manager(home, target, "directory")

    pending = manager.begin("skill_manage", {"action": "create"}, "call-1")
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("created", encoding="utf-8")
    snapshot = manager.finalize("call-1", '{"success": true}', status="ok")
    assert snapshot is not None

    manager.rollback(
        pending["snapshot_id"],
        expected_after_hash=snapshot["after_hash"],
    )

    assert target.exists() is False


def test_native_snapshot_does_not_treat_staged_write_as_applied(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("unchanged", encoding="utf-8")
    manager = _manager(home, target, "file")

    manager.begin("memory", {"action": "add"}, "call-1")
    snapshot = manager.finalize(
        "call-1",
        '{"success": true, "staged": true, "pending_id": "pending-1"}',
        status="ok",
    )

    assert snapshot is not None
    assert snapshot["applied"] is False
    assert snapshot["rollback_supported"] is False
    assert snapshot["reason"] == "write_staged"


def test_accepted_snapshot_is_collected_only_after_retention(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    manager = _manager(home, target, "file")

    pending = manager.begin("memory", {"action": "replace"}, "call-1")
    target.write_text("after", encoding="utf-8")
    snapshot = manager.finalize("call-1", '{"success": true}', status="ok")
    assert snapshot is not None
    accepted = manager.mark_accepted(
        pending["snapshot_id"],
        expected_after_hash=snapshot["after_hash"],
        retention_days=30,
    )
    accepted_again = manager.mark_accepted(
        pending["snapshot_id"],
        expected_after_hash=snapshot["after_hash"],
        retention_days=30,
    )
    deadline = datetime.fromisoformat(accepted["retention_until"])

    before_deadline = manager.collect_garbage(now=deadline - timedelta(seconds=1))
    at_deadline = manager.collect_garbage(now=deadline)

    assert accepted_again["idempotent"] is True
    assert accepted_again["retention_until"] == accepted["retention_until"]
    assert before_deadline["deleted"] == 0
    assert at_deadline["deleted"] == 1
    assert manager.usage()["snapshot_count"] == 0


def test_unreviewed_snapshot_is_never_automatically_collected(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    manager = _manager(home, target, "file")

    manager.begin("memory", {"action": "replace"}, "call-1")
    target.write_text("after", encoding="utf-8")
    manager.finalize("call-1", '{"success": true}', status="ok")

    result = manager.collect_garbage(now=datetime.now(UTC) + timedelta(days=4_000))

    assert result["eligible"] == 0
    assert result["deleted"] == 0
    assert manager.usage()["state_counts"] == {"ready": 1}


def test_terminal_and_no_change_snapshots_use_bounded_retention(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    manager = _manager(
        home,
        target,
        "file",
        terminal_retention_days=2,
        no_change_retention_hours=1,
    )

    pending = manager.begin("memory", {"action": "replace"}, "call-1")
    target.write_text("after", encoding="utf-8")
    snapshot = manager.finalize("call-1", '{"success": true}', status="ok")
    assert snapshot is not None
    manager.rollback(
        pending["snapshot_id"],
        expected_after_hash=snapshot["after_hash"],
    )

    manager.begin("memory", {"action": "replace"}, "call-2")
    unchanged = manager.finalize("call-2", '{"success": true}', status="ok")
    assert unchanged is not None
    assert unchanged["reason"] == "no_content_change"

    result = manager.collect_garbage(now=datetime.now(UTC) + timedelta(days=3))

    assert result["deleted"] == 2
    assert manager.usage()["snapshot_count"] == 0


def test_capacity_failure_cleans_partial_snapshot_and_releases_target(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("x" * 1_800, encoding="utf-8")
    manager = _manager(home, target, "file", max_total_bytes=2_048)

    with pytest.raises(NativeSnapshotError, match="capacity would be exceeded"):
        manager.begin("memory", {"action": "replace"}, "call-1")

    assert manager.usage()["snapshot_count"] == 0
    target.write_text("small", encoding="utf-8")
    pending = manager.begin("memory", {"action": "replace"}, "call-2")
    assert pending["snapshot_id"]
    assert manager.health()["backup"] == {
        "relative_path": ".hermesgraph/native_snapshots",
        "included_in_hermes_full_backup": True,
    }


def test_native_admin_routes_require_auth_and_register_acceptance(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    target = home / "memories" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    manager = _manager(home, target, "file")
    pending = manager.begin("memory", {"action": "replace"}, "call-1")
    target.write_text("after", encoding="utf-8")
    snapshot = manager.finalize("call-1", '{"success": true}', status="ok")
    assert snapshot is not None
    token = "test-native-admin-token"
    try:
        server = start_admin_server(manager, host="127.0.0.1", port=0, token=token)
    except PermissionError:
        pytest.skip("Local socket binding is disabled in this test environment")
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with httpx.Client(base_url=base_url, timeout=2) as client:
            unauthorized = client.get("/health")
            health = client.get(
                "/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            accepted = client.post(
                f"/v1/native-snapshots/{pending['snapshot_id']}/accepted",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "expected_after_hash": snapshot["after_hash"],
                    "retention_days": 30,
                },
            )
            gc_preview = client.post(
                "/v1/native-snapshots/gc",
                headers={"Authorization": f"Bearer {token}"},
                json={"dry_run": True},
            )
    finally:
        server.shutdown()
        server.server_close()

    assert unauthorized.status_code == 401
    assert health.status_code == 200
    assert health.json()["storage"]["snapshot_count"] == 1
    assert accepted.status_code == 200
    assert accepted.json()["review_state"] == "accepted"
    assert gc_preview.status_code == 200
    assert gc_preview.json()["deleted"] == 0


@pytest.mark.asyncio
async def test_native_admin_client_validates_health_and_acceptance() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "service": "hermesgraph-native-learning-admin",
                    "pending_mutations": 0,
                    "active_rollbacks": 0,
                    "storage": {"capacity_status": "ok"},
                    "backup": {"included_in_hermes_full_backup": True},
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "snapshot_id": "1" * 32,
                "review_state": "accepted",
                "retention_until": "2030-01-01T00:00:00+00:00",
                "idempotent": False,
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://native-admin",
    )
    admin = HermesNativeAdminClient(
        Settings(hermes_native_admin_token="test-token"),
        client=client,
    )

    health = await admin.health()
    accepted = await admin.mark_accepted(
        "1" * 32,
        expected_after_hash="2" * 64,
        retention_days=30,
    )
    await client.aclose()

    assert health.storage["capacity_status"] == "ok"
    assert accepted.retention_until.year == 2030


class _FakeNativeAdmin:
    def __init__(
        self,
        *,
        conflict: bool = False,
        acceptance_conflict: bool = False,
        retention_until: datetime | None = None,
    ) -> None:
        self.conflict = conflict
        self.acceptance_conflict = acceptance_conflict
        self.retention_until = retention_until
        self.rollback_calls: list[tuple[str, str]] = []
        self.accepted_calls: list[tuple[str, str, int]] = []

    async def health(self) -> HermesNativeAdminHealth:
        return HermesNativeAdminHealth(
            status="ok",
            service="test",
            pending_mutations=0,
            active_rollbacks=0,
            storage={"capacity_status": "ok"},
            backup={"included_in_hermes_full_backup": True},
        )

    async def mark_accepted(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
        retention_days: int,
    ) -> HermesNativeAcceptanceResult:
        self.accepted_calls.append((snapshot_id, expected_after_hash, retention_days))
        if self.acceptance_conflict:
            raise HermesNativeLearningConflict("snapshot missing")
        return HermesNativeAcceptanceResult(
            success=True,
            snapshot_id=snapshot_id,
            review_state="accepted",
            retention_until=(
                self.retention_until
                or datetime.now(UTC) + timedelta(days=retention_days)
            ),
        )

    async def rollback(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
    ) -> HermesNativeRollbackResult:
        self.rollback_calls.append((snapshot_id, expected_after_hash))
        if self.conflict:
            raise HermesNativeLearningConflict("target drift")
        return HermesNativeRollbackResult(
            success=True,
            snapshot_id=snapshot_id,
            state="rolled_back",
        )


async def _native_change(repository: JsonLearningChangeSetRepository) -> LearningChangeSet:
    run_id = uuid4()
    return await repository.save(
        LearningChangeSet(
            target_type="hermes_native_memory",
            target_id="memory",
            structured_diff={
                "runtime": "hermes",
                "state": "native_applied",
                "tool": "memory",
                "arguments": {
                    "action": "add",
                    "content": {"redacted": True, "length": 20, "sha256": "a" * 64},
                },
                "result": '{"success": true}',
                "snapshot": {
                    "snapshot_id": "1" * 32,
                    "target_kind": "memory",
                    "target_id": "memory",
                    "before_hash": "2" * 64,
                    "after_hash": "3" * 64,
                    "applied": True,
                    "rollback_supported": True,
                    "reason": None,
                },
            },
            source_run_ids=[run_id],
            scope={"tenant_id": "local", "project_id": "default"},
        )
    )


@pytest.mark.asyncio
async def test_native_learning_review_is_append_only_and_idempotent(tmp_path: Path) -> None:
    repository = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    base = await _native_change(repository)
    admin = _FakeNativeAdmin()
    service = HermesNativeLearningService(change_sets=repository, admin_client=admin)

    accepted = await service.review(
        base.change_set_id,
        "accept",
        reviewer_id="reviewer",
        reason="verified",
    )
    accepted_again = await service.review(
        base.change_set_id,
        "accept",
        reviewer_id="reviewer",
    )
    rolled_back = await service.review(
        base.change_set_id,
        "rollback",
        reviewer_id="reviewer",
        reason="superseded",
    )

    assert accepted.status == "accepted"
    assert accepted.snapshot_retention_until is not None
    assert len(accepted_again.reviews) == 1
    assert rolled_back.status == "rolled_back"
    assert len(rolled_back.reviews) == 2
    assert admin.accepted_calls == [("1" * 32, "3" * 64, 30)]
    assert admin.rollback_calls == [("1" * 32, "3" * 64)]
    assert len(await repository.list_all()) == 3


@pytest.mark.asyncio
async def test_native_learning_health_is_forwarded(tmp_path: Path) -> None:
    repository = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    service = HermesNativeLearningService(
        change_sets=repository,
        admin_client=_FakeNativeAdmin(),
    )

    health = await service.health()

    assert health.storage["capacity_status"] == "ok"
    assert health.backup["included_in_hermes_full_backup"] is True


@pytest.mark.asyncio
async def test_expired_acceptance_disables_rollback_in_audit(tmp_path: Path) -> None:
    repository = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    base = await _native_change(repository)
    service = HermesNativeLearningService(
        change_sets=repository,
        admin_client=_FakeNativeAdmin(
            retention_until=datetime.now(UTC) - timedelta(seconds=1)
        ),
    )

    accepted = await service.review(base.change_set_id, "accept")

    assert accepted.status == "accepted"
    assert accepted.rollback_supported is False
    assert accepted.rollback_reason == "accepted_snapshot_retention_expired"


@pytest.mark.asyncio
async def test_acceptance_registration_failure_keeps_review_but_no_gc_deadline(
    tmp_path: Path,
) -> None:
    repository = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    base = await _native_change(repository)
    service = HermesNativeLearningService(
        change_sets=repository,
        admin_client=_FakeNativeAdmin(acceptance_conflict=True),
    )

    accepted = await service.review(base.change_set_id, "accept")

    assert accepted.status == "accepted"
    assert accepted.snapshot_retention_until is None
    assert accepted.rollback_supported is True
    assert accepted.reviews[0].detail == (
        "Snapshot lifecycle registration deferred: snapshot missing"
    )


@pytest.mark.asyncio
async def test_native_learning_records_failed_rollback_attempt(tmp_path: Path) -> None:
    repository = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    base = await _native_change(repository)
    service = HermesNativeLearningService(
        change_sets=repository,
        admin_client=_FakeNativeAdmin(conflict=True),
    )

    with pytest.raises(HermesNativeLearningConflict, match="target drift"):
        await service.review(base.change_set_id, "rollback", reviewer_id="reviewer")

    audit = (await service.list_audits())[0]
    assert audit.status == "rollback_failed"
    assert audit.reviews[0].detail == "target drift"

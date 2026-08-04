from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.domain.contracts import LearningChangeSetRepository
from app.domain.models import LearningChangeSet

HermesNativeReviewDecision = Literal["accept", "rollback"]
HermesNativeLearningStatus = Literal[
    "pending",
    "accepted",
    "rolled_back",
    "rollback_failed",
]


class HermesNativeLearningError(RuntimeError):
    pass


class HermesNativeLearningConflict(HermesNativeLearningError):
    pass


class HermesNativeLearningUnavailable(HermesNativeLearningError):
    pass


class HermesNativeReviewEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_change_set_id: UUID
    decision: HermesNativeReviewDecision
    outcome: Literal["accepted", "rolled_back", "rollback_failed"]
    reviewer_id: str
    reason: str = ""
    detail: str | None = None
    retention_until: datetime | None = None
    created_at: datetime


class HermesNativeLearningAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_set_id: UUID
    target_type: Literal["hermes_native_memory", "hermes_native_skill"]
    target_id: str
    action: str
    source_run_ids: list[UUID]
    status: HermesNativeLearningStatus
    snapshot_id: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    rollback_supported: bool = False
    rollback_reason: str | None = None
    snapshot_retention_until: datetime | None = None
    sanitized_arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: str = ""
    scope: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    reviews: list[HermesNativeReviewEvent] = Field(default_factory=list)


class HermesNativeRollbackResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    success: bool
    snapshot_id: str
    state: Literal["rolled_back"]
    current_hash: str | None = None
    idempotent: bool = False


class HermesNativeAcceptanceResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    success: bool
    snapshot_id: str
    review_state: Literal["accepted"]
    retention_until: datetime
    idempotent: bool = False


class HermesNativeAdminHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]
    service: str
    pending_mutations: int = Field(ge=0)
    active_rollbacks: int = Field(ge=0)
    storage: dict[str, Any]
    backup: dict[str, Any]


class HermesNativeAdminPort(Protocol):
    async def health(self) -> HermesNativeAdminHealth: ...

    async def mark_accepted(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
        retention_days: int,
    ) -> HermesNativeAcceptanceResult: ...

    async def rollback(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
    ) -> HermesNativeRollbackResult: ...


class HermesNativeAdminClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        token = settings.hermes_native_admin_token
        if token is None:
            raise ValueError("HERMES_NATIVE_ADMIN_TOKEN is required")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.hermes_native_admin_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            timeout=settings.hermes_native_admin_timeout_seconds,
        )

    async def start(self) -> None:
        await self.health()

    async def health(self) -> HermesNativeAdminHealth:
        try:
            response = await self._client.get("/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin is unavailable"
            ) from exc
        try:
            return HermesNativeAdminHealth.model_validate(response.json())
        except ValueError as exc:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin returned an invalid response"
            ) from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def rollback(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
    ) -> HermesNativeRollbackResult:
        try:
            response = await self._client.post(
                f"/v1/native-snapshots/{snapshot_id}/rollback",
                json={"expected_after_hash": expected_after_hash},
            )
        except httpx.HTTPError as exc:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin is unavailable"
            ) from exc
        if response.status_code in {404, 409}:
            raise HermesNativeLearningConflict(_response_error(response))
        if response.status_code >= 400:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin rejected the rollback"
            )
        try:
            return HermesNativeRollbackResult.model_validate(response.json())
        except ValueError as exc:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin returned an invalid response"
            ) from exc

    async def mark_accepted(
        self,
        snapshot_id: str,
        *,
        expected_after_hash: str,
        retention_days: int,
    ) -> HermesNativeAcceptanceResult:
        try:
            response = await self._client.post(
                f"/v1/native-snapshots/{snapshot_id}/accepted",
                json={
                    "expected_after_hash": expected_after_hash,
                    "retention_days": retention_days,
                },
            )
        except httpx.HTTPError as exc:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin is unavailable"
            ) from exc
        if response.status_code in {404, 409}:
            raise HermesNativeLearningConflict(_response_error(response))
        if response.status_code >= 400:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin rejected the acceptance"
            )
        try:
            return HermesNativeAcceptanceResult.model_validate(response.json())
        except ValueError as exc:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin returned an invalid response"
            ) from exc


class HermesNativeLearningService:
    _NATIVE_TYPES = {"hermes_native_memory", "hermes_native_skill"}
    _REVIEW_TYPE = "hermes_native_review"

    def __init__(
        self,
        *,
        change_sets: LearningChangeSetRepository,
        admin_client: HermesNativeAdminPort | None,
        snapshot_retention_days: int = 30,
    ) -> None:
        if not 1 <= snapshot_retention_days <= 3_650:
            raise ValueError("Snapshot retention must be between 1 and 3650 days")
        self._change_sets = change_sets
        self._admin = admin_client
        self._snapshot_retention_days = snapshot_retention_days
        self._review_lock = asyncio.Lock()

    async def health(self) -> HermesNativeAdminHealth:
        if self._admin is None:
            raise HermesNativeLearningUnavailable(
                "Hermes native learning admin is not configured"
            )
        return await self._admin.health()

    async def list_audits(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> list[HermesNativeLearningAudit]:
        changes = [
            item
            for item in await self._change_sets.list_all()
            if item.scope.get("tenant_id", "local") == tenant_id
            and item.scope.get("project_id", "default") == project_id
        ]
        reviews: dict[str, list[LearningChangeSet]] = {}
        for item in changes:
            if item.target_type == self._REVIEW_TYPE:
                reviews.setdefault(item.target_id, []).append(item)

        audits = [
            self._build_audit(item, reviews.get(str(item.change_set_id), []))
            for item in changes
            if item.target_type in self._NATIVE_TYPES
        ]
        return sorted(audits, key=lambda item: item.created_at, reverse=True)

    async def review(
        self,
        change_set_id: UUID,
        decision: HermesNativeReviewDecision,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        reviewer_id: str = "local-user",
        reason: str = "",
    ) -> HermesNativeLearningAudit:
        async with self._review_lock:
            audit = await self._get_audit(
                change_set_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            base = await self._get_base_change_set(
                change_set_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if decision == "accept":
                if audit.status == "accepted":
                    return audit
                if audit.status == "rolled_back":
                    raise HermesNativeLearningConflict(
                        "A rolled-back native learning change cannot be accepted"
                    )
                retention_until: datetime | None = None
                detail: str | None = None
                if (
                    self._admin is not None
                    and audit.rollback_supported
                    and audit.snapshot_id
                    and audit.after_hash
                ):
                    try:
                        accepted = await self._admin.mark_accepted(
                            audit.snapshot_id,
                            expected_after_hash=audit.after_hash,
                            retention_days=self._snapshot_retention_days,
                        )
                        retention_until = accepted.retention_until
                    except (
                        HermesNativeLearningConflict,
                        HermesNativeLearningUnavailable,
                    ) as exc:
                        detail = f"Snapshot lifecycle registration deferred: {exc}"
                await self._append_review(
                    base,
                    decision=decision,
                    outcome="accepted",
                    reviewer_id=reviewer_id,
                    reason=reason,
                    detail=detail,
                    retention_until=retention_until,
                )
                return await self._get_audit(
                    change_set_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                )

            if audit.status == "rolled_back":
                return audit
            if (
                not audit.rollback_supported
                or not audit.snapshot_id
                or not audit.after_hash
            ):
                detail = audit.rollback_reason or "No verified native snapshot is available"
                await self._append_review(
                    base,
                    decision=decision,
                    outcome="rollback_failed",
                    reviewer_id=reviewer_id,
                    reason=reason,
                    detail=detail,
                )
                raise HermesNativeLearningConflict(detail)
            if self._admin is None:
                detail = "Hermes native learning admin is not configured"
                await self._append_review(
                    base,
                    decision=decision,
                    outcome="rollback_failed",
                    reviewer_id=reviewer_id,
                    reason=reason,
                    detail=detail,
                )
                raise HermesNativeLearningUnavailable(detail)

            try:
                rollback = await self._admin.rollback(
                    audit.snapshot_id,
                    expected_after_hash=audit.after_hash,
                )
            except (HermesNativeLearningConflict, HermesNativeLearningUnavailable) as exc:
                await self._append_review(
                    base,
                    decision=decision,
                    outcome="rollback_failed",
                    reviewer_id=reviewer_id,
                    reason=reason,
                    detail=str(exc),
                )
                raise
            await self._append_review(
                base,
                decision=decision,
                outcome="rolled_back",
                reviewer_id=reviewer_id,
                reason=reason,
                detail="idempotent" if rollback.idempotent else None,
                rollback_result=rollback.model_dump(mode="json"),
            )
            return await self._get_audit(
                change_set_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )

    async def _get_audit(
        self,
        change_set_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HermesNativeLearningAudit:
        for audit in await self.list_audits(
            tenant_id=tenant_id,
            project_id=project_id,
        ):
            if audit.change_set_id == change_set_id:
                return audit
        raise KeyError("Hermes native learning change was not found")

    async def _get_base_change_set(
        self,
        change_set_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningChangeSet:
        for item in await self._change_sets.list_all():
            if (
                item.change_set_id == change_set_id
                and item.target_type in self._NATIVE_TYPES
                and item.scope.get("tenant_id", "local") == tenant_id
                and item.scope.get("project_id", "default") == project_id
            ):
                return item
        raise KeyError("Hermes native learning change was not found")

    async def _append_review(
        self,
        base: LearningChangeSet,
        *,
        decision: HermesNativeReviewDecision,
        outcome: Literal["accepted", "rolled_back", "rollback_failed"],
        reviewer_id: str,
        reason: str,
        detail: str | None = None,
        retention_until: datetime | None = None,
        rollback_result: dict[str, Any] | None = None,
    ) -> None:
        await self._change_sets.save(
            LearningChangeSet(
                target_type=self._REVIEW_TYPE,
                target_id=str(base.change_set_id),
                structured_diff={
                    "runtime": "hermes",
                    "decision": decision,
                    "outcome": outcome,
                    "reviewer_id": reviewer_id,
                    "reason": reason,
                    "detail": detail,
                    "retention_until": (
                        retention_until.isoformat() if retention_until is not None else None
                    ),
                    "rollback_result": rollback_result,
                },
                source_run_ids=base.source_run_ids,
                expected_benefits=["Keep Hermes native learning governed and reversible"],
                risks=(
                    ["Rollback did not complete"]
                    if outcome == "rollback_failed"
                    else []
                ),
                scope=dict(base.scope),
                evaluation_report={
                    "status": outcome,
                    "runtime": "hermes",
                    "reviewed": True,
                },
                rollback_conditions=[],
            )
        )

    @staticmethod
    def _build_audit(
        base: LearningChangeSet,
        review_changes: list[LearningChangeSet],
    ) -> HermesNativeLearningAudit:
        diff = base.structured_diff
        raw_snapshot = diff.get("snapshot")
        snapshot: dict[str, Any] = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        reviews: list[HermesNativeReviewEvent] = []
        status: HermesNativeLearningStatus = "pending"
        retention_until: datetime | None = None
        for change in sorted(review_changes, key=lambda item: item.created_at):
            review = change.structured_diff
            outcome = str(review.get("outcome") or "rollback_failed")
            decision = str(review.get("decision") or "")
            if (
                outcome not in {"accepted", "rolled_back", "rollback_failed"}
                or decision not in {"accept", "rollback"}
            ):
                continue
            status = outcome  # type: ignore[assignment]
            raw_retention = review.get("retention_until")
            parsed_retention: datetime | None = None
            if isinstance(raw_retention, str):
                try:
                    parsed_retention = datetime.fromisoformat(raw_retention)
                except ValueError:
                    parsed_retention = None
            if parsed_retention is not None:
                retention_until = parsed_retention
            reviews.append(
                HermesNativeReviewEvent(
                    review_change_set_id=change.change_set_id,
                    decision=decision,
                    outcome=outcome,
                    reviewer_id=str(review.get("reviewer_id") or "unknown"),
                    reason=str(review.get("reason") or ""),
                    detail=(str(review["detail"]) if review.get("detail") else None),
                    retention_until=parsed_retention,
                    created_at=change.created_at,
                )
            )
        arguments = diff.get("arguments")
        rollback_supported = bool(snapshot.get("rollback_supported"))
        rollback_reason = str(snapshot["reason"]) if snapshot.get("reason") else None
        if status == "rolled_back":
            rollback_supported = False
            rollback_reason = "native_snapshot_already_rolled_back"
        elif retention_until is not None:
            effective_deadline = (
                retention_until
                if retention_until.tzinfo is not None
                else retention_until.replace(tzinfo=UTC)
            )
            if datetime.now(UTC) >= effective_deadline:
                rollback_supported = False
                rollback_reason = "accepted_snapshot_retention_expired"
        return HermesNativeLearningAudit(
            change_set_id=base.change_set_id,
            target_type=base.target_type,
            target_id=base.target_id,
            action=str(arguments.get("action") or "unknown")
            if isinstance(arguments, dict)
            else "unknown",
            source_run_ids=base.source_run_ids,
            status=status,
            snapshot_id=(str(snapshot["snapshot_id"]) if snapshot.get("snapshot_id") else None),
            before_hash=(str(snapshot["before_hash"]) if snapshot.get("before_hash") else None),
            after_hash=(str(snapshot["after_hash"]) if snapshot.get("after_hash") else None),
            rollback_supported=rollback_supported,
            rollback_reason=rollback_reason,
            snapshot_retention_until=retention_until,
            sanitized_arguments=arguments if isinstance(arguments, dict) else {},
            result_summary=str(diff.get("result") or ""),
            scope=dict(base.scope),
            created_at=base.created_at,
            reviews=reviews,
        )


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "Hermes native snapshot operation was rejected"
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])[:1_000]
    return "Hermes native snapshot operation was rejected"

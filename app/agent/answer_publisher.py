import hashlib
from collections.abc import Collection, Sequence
from uuid import UUID

from app.domain.enums import AnswerMode, EvidenceLevel, TrustLevel
from app.domain.models import (
    AgentAnswerDraft,
    AnswerResponse,
    Claim,
    EvidenceRef,
    FollowUpAction,
    GraphPath,
)


class EvidencePublicationError(ValueError):
    pass


class AnswerPublisher:
    """Deterministic final gate between model output and user-visible claims."""

    def publish(
        self,
        draft: AgentAnswerDraft,
        *,
        allowed_evidence: Sequence[EvidenceRef],
        graph_paths: Sequence[GraphPath] = (),
    ) -> AnswerResponse:
        evidence_by_id = {item.evidence_id: item for item in allowed_evidence}
        requested_ids = set(draft.citation_ids)
        unknown = requested_ids - evidence_by_id.keys()
        if unknown:
            raise EvidencePublicationError(
                f"Answer cited evidence outside this run: {unknown}"
            )
        citations: list[EvidenceRef] = []
        seen: set[UUID] = set()
        for evidence_id in draft.citation_ids:
            if evidence_id in seen:
                continue
            citations.append(evidence_by_id[evidence_id])
            seen.add(evidence_id)
        claims = self._normalize_claim_trust(draft.claims, evidence_by_id)
        confidence = draft.confidence
        if draft.response_mode != AnswerMode.GROUNDED:
            # Evidence confidence is not applicable to social conversation
            # or action receipts, even if the model supplied evidence fields.
            claims = []
            citations = []
            confidence = EvidenceLevel.INSUFFICIENT
        if (
            confidence == EvidenceLevel.VERIFIED
            and citations
            and any(
                item.provenance.trust != TrustLevel.VERIFIED
                for item in citations
            )
        ):
            confidence = EvidenceLevel.SUPPORTED
        answer = AnswerResponse(
            answer_markdown=draft.answer_markdown,
            response_mode=draft.response_mode,
            claims=claims,
            citations=citations,
            confidence=confidence,
            limitations=draft.limitations,
            followup_queries=draft.followup_queries,
            graph_paths=list(graph_paths) if draft.response_mode == AnswerMode.GROUNDED else [],
            follow_up_actions=self._follow_up_actions(draft.followup_queries),
        )
        return self.validate(answer, allowed_evidence_ids=evidence_by_id.keys())

    def hydrate_view(self, answer: AnswerResponse) -> AnswerResponse:
        """Project model-independent read-only actions at the server boundary.

        Graph paths are intentionally untouched here. They can only be added by
        the Hermes bridge after a scoped graph tool has returned them.
        """

        return answer.model_copy(
            update={"follow_up_actions": self._follow_up_actions(answer.followup_queries)}
        )

    def validate(
        self,
        answer: AnswerResponse,
        *,
        allowed_evidence_ids: Collection[UUID],
    ) -> AnswerResponse:
        allowed = set(allowed_evidence_ids)
        cited = {citation.evidence_id for citation in answer.citations}
        unknown = cited - allowed
        if unknown:
            raise EvidencePublicationError(f"Answer cited evidence outside this run: {unknown}")

        graph_evidence = {
            evidence.evidence_id
            for path in answer.graph_paths
            for evidence in _path_evidence(path)
        }
        unknown_graph_evidence = graph_evidence - allowed
        if unknown_graph_evidence:
            raise EvidencePublicationError(
                "Graph paths refer to evidence outside this run: "
                f"{unknown_graph_evidence}"
            )
        malformed_paths = [
            path
            for path in answer.graph_paths
            if not path.nodes or not path.relationships
        ]
        if malformed_paths:
            raise EvidencePublicationError(
                "Published graph paths must contain nodes and relationships"
            )

        claim_evidence = {
            evidence_id for claim in answer.claims for evidence_id in claim.evidence_ids
        }
        unknown_claim_evidence = claim_evidence - cited
        if unknown_claim_evidence:
            raise EvidencePublicationError(
                f"Claims refer to citations absent from the answer: {unknown_claim_evidence}"
            )

        supported_claims = [
            claim
            for claim in answer.claims
            if claim.level in {EvidenceLevel.VERIFIED, EvidenceLevel.SUPPORTED}
        ]
        unsupported_supported_claims = [
            claim for claim in supported_claims if not claim.evidence_ids
        ]
        if unsupported_supported_claims:
            raise EvidencePublicationError("Supported claims must include at least one evidence ID")

        if (
            answer.confidence in {EvidenceLevel.VERIFIED, EvidenceLevel.SUPPORTED}
            and not answer.citations
        ):
            raise EvidencePublicationError("A supported answer must contain citations")
        citation_by_id = {
            citation.evidence_id: citation
            for citation in answer.citations
        }
        invalid_verified_claims = [
            claim
            for claim in answer.claims
            if claim.level == EvidenceLevel.VERIFIED
            and any(
                citation_by_id[evidence_id].provenance.trust
                != TrustLevel.VERIFIED
                for evidence_id in claim.evidence_ids
                if evidence_id in citation_by_id
            )
        ]
        if invalid_verified_claims:
            raise EvidencePublicationError(
                "Verified claims require verified-trust evidence"
            )
        if (
            answer.confidence == EvidenceLevel.VERIFIED
            and any(
                citation.provenance.trust != TrustLevel.VERIFIED
                for citation in answer.citations
            )
        ):
            raise EvidencePublicationError(
                "Verified confidence requires verified-trust citations"
            )
        return answer

    @staticmethod
    def _follow_up_actions(queries: Sequence[str]) -> list[FollowUpAction]:
        actions: list[FollowUpAction] = []
        seen: set[str] = set()
        for raw_query in queries:
            query = " ".join(raw_query.split())
            if not query or len(query) > 2_000 or query in seen:
                continue
            seen.add(query)
            action_id = hashlib.sha256(query.encode()).hexdigest()[:16]
            actions.append(
                FollowUpAction(
                    action_id=f"query:{action_id}",
                    label=query,
                    query=query,
                )
            )
        return actions

    @staticmethod
    def _normalize_claim_trust(
        claims: Sequence[Claim],
        evidence_by_id: dict[UUID, EvidenceRef],
    ) -> list[Claim]:
        normalized: list[Claim] = []
        for claim in claims:
            supporting = [
                evidence_by_id[evidence_id]
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence_by_id
            ]
            if (
                claim.level == EvidenceLevel.VERIFIED
                and supporting
                and any(
                    item.provenance.trust != TrustLevel.VERIFIED
                    for item in supporting
                )
            ):
                claim = claim.model_copy(update={"level": EvidenceLevel.SUPPORTED})
            normalized.append(claim)
        return normalized


def _path_evidence(path: GraphPath) -> list[EvidenceRef]:
    return [
        *path.evidence,
        *(evidence for relationship in path.relationships for evidence in relationship.evidence),
    ]

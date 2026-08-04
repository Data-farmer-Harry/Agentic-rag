from uuid import uuid4

import pytest

from app.domain.enums import AnswerMode, EvidenceLevel, TrustLevel
from app.domain.models import AgentAnswerDraft, AnswerResponse, Claim, EvidenceRef, Provenance
from app.evidence.publisher import AnswerPublisher, EvidencePublicationError


def evidence() -> EvidenceRef:
    return EvidenceRef(
        text="A source-backed statement.",
        provenance=Provenance(source_type="fixture", source_id="doc-1", trust=TrustLevel.VERIFIED),
    )


def test_publisher_accepts_run_local_citation() -> None:
    item = evidence()
    answer = AnswerResponse(
        answer_markdown="Supported.",
        claims=[
            Claim(
                text="Supported claim",
                evidence_ids=[item.evidence_id],
                level=EvidenceLevel.SUPPORTED,
            )
        ],
        citations=[item],
        confidence=EvidenceLevel.SUPPORTED,
    )

    assert AnswerPublisher().validate(answer, allowed_evidence_ids=[item.evidence_id]) == answer


def test_publisher_rejects_foreign_citation() -> None:
    item = evidence()
    answer = AnswerResponse(
        answer_markdown="Unsupported.",
        citations=[item],
        confidence=EvidenceLevel.SUPPORTED,
    )

    with pytest.raises(EvidencePublicationError):
        AnswerPublisher().validate(answer, allowed_evidence_ids=[uuid4()])


def test_publisher_hydrates_only_allowlisted_citations() -> None:
    item = evidence()
    draft = AgentAnswerDraft(
        answer_markdown="Supported.",
        claims=[
            Claim(
                text="Supported claim",
                evidence_ids=[item.evidence_id],
                level=EvidenceLevel.SUPPORTED,
            )
        ],
        citation_ids=[item.evidence_id, item.evidence_id],
        confidence=EvidenceLevel.SUPPORTED,
    )

    answer = AnswerPublisher().publish(draft, allowed_evidence=[item])

    assert answer.citations == [item]


def test_publisher_preserves_conversational_mode_without_citations() -> None:
    draft = AgentAnswerDraft(
        answer_markdown="你好！有什么我可以帮你的吗？",
        response_mode=AnswerMode.CONVERSATIONAL,
        confidence=EvidenceLevel.SUPPORTED,
        claims=[
            Claim(
                text="Model-authored social claim.",
                level=EvidenceLevel.SUPPORTED,
            )
        ],
    )

    answer = AnswerPublisher().publish(draft, allowed_evidence=[])

    assert answer.response_mode == AnswerMode.CONVERSATIONAL
    assert answer.confidence == EvidenceLevel.INSUFFICIENT
    assert answer.claims == []
    assert answer.citations == []


def test_publisher_rejects_model_authored_citation_id() -> None:
    draft = AgentAnswerDraft(answer_markdown="Invented.", citation_ids=[uuid4()])

    with pytest.raises(EvidencePublicationError):
        AnswerPublisher().publish(draft, allowed_evidence=[])


def test_publisher_downgrades_untrusted_web_evidence_from_verified() -> None:
    item = EvidenceRef(
        text="A provider-synthesized web citation context.",
        provenance=Provenance(
            source_type="web_search",
            source_id="https://example.com/source",
            trust=TrustLevel.UNTRUSTED,
        ),
    )
    draft = AgentAnswerDraft(
        answer_markdown="Cited, but not independently verified.",
        claims=[
            Claim(
                text="Web-backed claim",
                evidence_ids=[item.evidence_id],
                level=EvidenceLevel.VERIFIED,
            )
        ],
        citation_ids=[item.evidence_id],
        confidence=EvidenceLevel.VERIFIED,
    )

    answer = AnswerPublisher().publish(draft, allowed_evidence=[item])

    assert answer.claims[0].level == EvidenceLevel.SUPPORTED
    assert answer.confidence == EvidenceLevel.SUPPORTED


def test_publisher_rejects_verified_label_on_untrusted_citation() -> None:
    item = EvidenceRef(
        text="Untrusted evidence.",
        provenance=Provenance(
            source_type="web_search",
            source_id="https://example.com/source",
        ),
    )
    answer = AnswerResponse(
        answer_markdown="Incorrectly labeled as verified.",
        claims=[
            Claim(
                text="Claim",
                evidence_ids=[item.evidence_id],
                level=EvidenceLevel.VERIFIED,
            )
        ],
        citations=[item],
        confidence=EvidenceLevel.VERIFIED,
    )

    with pytest.raises(EvidencePublicationError, match="Verified claims"):
        AnswerPublisher().validate(
            answer,
            allowed_evidence_ids=[item.evidence_id],
        )

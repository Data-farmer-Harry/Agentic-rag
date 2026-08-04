from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.enums import AnswerMode, EvidenceLevel, RunStatus, TrustLevel
from app.domain.models import EvidenceRef, RunTrajectory

LEARNING_GATE_REVISION = "citation-provenance-learning-gate-v1"

_KNOWN_SOURCE_LAYERS = frozenset(
    {
        "team_internal",
        "personal",
        "public_reference",
    }
)
_SECURITY_METADATA_KEYS = frozenset(
    {
        "security_signal",
        "security_signals",
        "security_flag",
        "security_flags",
        "safety_signal",
        "safety_signals",
        "source_security_signal",
        "source_security_signals",
        "security",
        "prompt_injection",
        "prompt_injection_detected",
        "has_prompt_injection",
        "injection_detected",
        "jailbreak_detected",
    }
)
_UNSAFE_SIGNAL_MARKERS = (
    "prompt_injection",
    "prompt injection",
    "injection",
    "jailbreak",
    "malicious_instruction",
    "malicious instruction",
    "untrusted_instruction",
    "untrusted instruction",
    "instruction_override",
    "instruction override",
    "secret_exfiltration",
    "secret exfiltration",
)
_PROMPT_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?\b",
        r"\breveal\s+(?:the\s+)?(?:system|developer)\s+prompt\b",
        r"\bact\s+as\s+(?:the\s+)?system\b",
        r"\bwrite\s+(?:this|it)\s+(?:to|into)\s+(?:permanent\s+)?memory\b",
        r"忽略.{0,12}(?:之前|先前|以上).{0,8}(?:指令|规则)",
        r"(?:写入|保存).{0,8}(?:永久)?记忆",
    )
)

_REASON_MESSAGES = {
    "run_status_failed": "the run failed",
    "run_status_cancelled": "the run was cancelled",
    "run_not_completed": "the run did not complete",
    "missing_final_answer": "the run has no final answer",
    "response_mode_conversational": "the answer is conversational rather than grounded",
    "response_mode_action": "the answer is an action receipt rather than grounded evidence",
    "answer_confidence_insufficient": "the final answer is insufficient",
    "answer_confidence_not_supported": "the final answer is not supported by evidence",
    "no_final_citations": "the grounded answer has no final citations",
    "no_valid_final_citations": "none of the final citations are eligible learning evidence",
    "citation_untrusted_evidence": "a final citation is untrusted",
    "citation_source_layer_missing_or_unknown": "a final citation has no recognized source layer",
    "citation_prompt_injection_detected": "a final citation contains a prompt-injection signal",
    "citation_provenance_incomplete": "a final citation has incomplete provenance",
}


@dataclass(frozen=True, slots=True)
class CitationLearningSignal:
    """The original provenance facts used for one final-citation gate decision."""

    evidence_id: str
    trust: TrustLevel
    source_layer: str | None
    security_signals: tuple[str, ...]
    provenance_complete: bool

    @property
    def eligible(self) -> bool:
        return (
            self.trust != TrustLevel.UNTRUSTED
            and self.source_layer in _KNOWN_SOURCE_LAYERS
            and not self.security_signals
            and self.provenance_complete
        )


@dataclass(frozen=True, slots=True)
class AutomaticLearningDecision:
    """Deterministic admission decision for all automatically derived artifacts."""

    allowed: bool
    reasons: tuple[str, ...]
    citation_signals: tuple[CitationLearningSignal, ...]

    @property
    def audit_summary(self) -> str:
        if self.allowed:
            return "Automatic learning is eligible from grounded final citations."
        explanations = [
            _REASON_MESSAGES.get(reason, reason.replace("_", " "))
            for reason in self.reasons
        ]
        return "Automatic learning skipped: " + "; ".join(explanations) + "."


def assess_automatic_learning(trajectory: RunTrajectory) -> AutomaticLearningDecision:
    """Fail closed using only the final answer and its actually cited evidence.

    Tool output may be untrusted, retrieved but not used, or later superseded.  Durable
    learning therefore derives its admission decision exclusively from the final
    ``AnswerResponse.citations`` and their server-originated provenance metadata.
    """

    reasons: list[str] = []
    answer = trajectory.answer
    if trajectory.status != RunStatus.COMPLETED:
        reasons.append(
            f"run_status_{trajectory.status.value}"
            if trajectory.status in {RunStatus.FAILED, RunStatus.CANCELLED}
            else "run_not_completed"
        )
    if answer is None:
        reasons.append("missing_final_answer")
        return AutomaticLearningDecision(
            allowed=False,
            reasons=tuple(dict.fromkeys(reasons)),
            citation_signals=(),
        )

    if answer.response_mode != AnswerMode.GROUNDED:
        reasons.append(f"response_mode_{answer.response_mode.value}")
    if answer.confidence == EvidenceLevel.INSUFFICIENT:
        reasons.append("answer_confidence_insufficient")
    elif answer.confidence not in {EvidenceLevel.SUPPORTED, EvidenceLevel.VERIFIED}:
        reasons.append("answer_confidence_not_supported")

    citations = tuple(answer.citations)
    if not citations:
        reasons.append("no_final_citations")
    signals = tuple(_citation_signal(citation) for citation in citations)
    if any(signal.trust == TrustLevel.UNTRUSTED for signal in signals):
        reasons.append("citation_untrusted_evidence")
    if any(signal.source_layer not in _KNOWN_SOURCE_LAYERS for signal in signals):
        reasons.append("citation_source_layer_missing_or_unknown")
    if any(signal.security_signals for signal in signals):
        reasons.append("citation_prompt_injection_detected")
    if any(not signal.provenance_complete for signal in signals):
        reasons.append("citation_provenance_incomplete")
    if citations and not any(signal.eligible for signal in signals):
        reasons.append("no_valid_final_citations")

    deduplicated = tuple(dict.fromkeys(reasons))
    return AutomaticLearningDecision(
        allowed=not deduplicated,
        reasons=deduplicated,
        citation_signals=signals,
    )


def annotate_trajectory_for_automatic_learning(
    trajectory: RunTrajectory,
    decision: AutomaticLearningDecision | None = None,
) -> tuple[RunTrajectory, AutomaticLearningDecision]:
    """Persist a compact, non-sensitive learning audit alongside the run."""

    resolved = decision or assess_automatic_learning(trajectory)
    retained = [
        tag
        for tag in trajectory.tags
        if tag != "learning_non_learnable"
        and not tag.startswith("learning_gate:")
        and not tag.startswith("learning_gate_reason:")
    ]
    audit_tags = [f"learning_gate:{LEARNING_GATE_REVISION}"]
    if resolved.allowed:
        audit_tags.append("learning_gate:eligible")
    else:
        audit_tags.append("learning_non_learnable")
        audit_tags.extend(
            f"learning_gate_reason:{reason}" for reason in resolved.reasons
        )
    return (
        trajectory.model_copy(update={"tags": list(dict.fromkeys([*retained, *audit_tags]))}),
        resolved,
    )


def _citation_signal(citation: EvidenceRef) -> CitationLearningSignal:
    metadata = citation.metadata
    source_layer = _source_layer(metadata)
    security_signals = _security_signals(citation)
    provenance = citation.provenance
    return CitationLearningSignal(
        evidence_id=str(citation.evidence_id),
        trust=provenance.trust,
        source_layer=source_layer,
        security_signals=security_signals,
        provenance_complete=bool(
            provenance.source_type.strip() and provenance.source_id.strip()
        ),
    )


def _source_layer(metadata: Mapping[str, Any]) -> str | None:
    for key in ("knowledge_layer", "source_layer"):
        value = metadata.get(key)
        if value is None:
            continue
        normalized = str(value).strip().casefold()
        return normalized or None
    return None


def _security_signals(citation: EvidenceRef) -> tuple[str, ...]:
    detected: set[str] = set()
    if any(pattern.search(citation.text) for pattern in _PROMPT_INJECTION_PATTERNS):
        detected.add("citation_text_prompt_injection")
    _collect_security_signals(citation.metadata, detected)
    _collect_security_signals(citation.provenance.locator, detected)
    return tuple(sorted(detected))


def _collect_security_signals(value: Any, detected: set[str], *, depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            if key in _SECURITY_METADATA_KEYS:
                if _value_has_unsafe_signal(item):
                    detected.add(f"metadata:{key}")
                continue
            _collect_security_signals(item, detected, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _collect_security_signals(item, detected, depth=depth + 1)


def _value_has_unsafe_signal(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        normalized = value.casefold()
        return any(marker in normalized for marker in _UNSAFE_SIGNAL_MARKERS)
    if isinstance(value, Mapping):
        return any(
            _value_has_unsafe_signal(item)
            or any(marker in str(key).casefold() for marker in _UNSAFE_SIGNAL_MARKERS)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_value_has_unsafe_signal(item) for item in value)
    return False


__all__ = [
    "AutomaticLearningDecision",
    "CitationLearningSignal",
    "LEARNING_GATE_REVISION",
    "annotate_trajectory_for_automatic_learning",
    "assess_automatic_learning",
]

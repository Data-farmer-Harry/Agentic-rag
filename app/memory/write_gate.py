from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain.contracts import MemoryRepository
from app.domain.enums import MemoryType, TrustLevel
from app.domain.models import MemoryCandidate, MemoryRecord

_TRUST_SCORE: Mapping[TrustLevel, float] = {
    TrustLevel.UNTRUSTED: 0.0,
    TrustLevel.USER_ASSERTED: 0.6,
    TrustLevel.OBSERVED: 0.8,
    TrustLevel.VERIFIED: 1.0,
}

_INJECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+)?(previous|prior)\s+instructions?",
        r"reveal\s+(the\s+)?(system|developer)\s+prompt",
        r"act\s+as\s+(the\s+)?system",
        r"write\s+(this|it)\s+(to|into)\s+(permanent\s+)?memory",
        r"忽略.{0,12}(之前|先前|以上).{0,8}(指令|规则)",
        r"写入.{0,8}(永久)?记忆",
    )
)

_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
        r"\b(?:api[_-]?key|secret|password|passwd)\s*[:=]\s*\S{8,}",
    )
)

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "private_key",
    "secret",
}


@dataclass(frozen=True, slots=True)
class MemoryWriteDecision:
    allowed: bool
    reasons: tuple[str, ...]


class MemoryWriteRejected(ValueError):
    def __init__(self, decision: MemoryWriteDecision) -> None:
        super().__init__("Memory candidate rejected: " + "; ".join(decision.reasons))
        self.decision = decision


class MemoryWriteGate:
    """Deterministic admission control for every durable memory mutation."""

    def __init__(
        self,
        *,
        min_confidence: float = 0.6,
        max_payload_bytes: int = 32_000,
        require_run_id: bool = True,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if max_payload_bytes < 256:
            raise ValueError("max_payload_bytes must be at least 256")
        self._min_confidence = min_confidence
        self._max_payload_bytes = max_payload_bytes
        self._require_run_id = require_run_id

    def evaluate(self, candidate: MemoryCandidate) -> MemoryWriteDecision:
        reasons: list[str] = []
        if candidate.confidence < self._min_confidence:
            reasons.append("confidence_below_threshold")
        if not candidate.key.strip() or not candidate.summary.strip():
            reasons.append("empty_key_or_summary")
        if candidate.expires_at is not None:
            expires_at = candidate.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                reasons.append("already_expired")

        provenance = candidate.provenance
        if any(not item.source_type.strip() or not item.source_id.strip() for item in provenance):
            reasons.append("incomplete_provenance")
        if self._require_run_id and not any(item.run_id is not None for item in provenance):
            reasons.append("missing_run_provenance")

        required_trust = {
            MemoryType.EPISODIC: 0.6,
            MemoryType.SEMANTIC: 0.8,
            MemoryType.PROCEDURAL: 0.8,
            MemoryType.POLICY: 1.0,
        }[candidate.memory_type]
        observed_trust = min((_TRUST_SCORE[item.trust] for item in provenance), default=0.0)
        if observed_trust < required_trust:
            reasons.append("insufficient_provenance_trust")

        payload = candidate.model_dump(mode="json")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode("utf-8")) > self._max_payload_bytes:
            reasons.append("payload_too_large")
        if any(pattern.search(serialized) for pattern in _INJECTION_PATTERNS):
            reasons.append("prompt_injection_pattern")
        if self._contains_sensitive_value(payload) or any(
            pattern.search(serialized) for pattern in _SECRET_PATTERNS
        ):
            reasons.append("sensitive_data")
        return MemoryWriteDecision(allowed=not reasons, reasons=tuple(dict.fromkeys(reasons)))

    def require(self, candidate: MemoryCandidate) -> None:
        decision = self.evaluate(candidate)
        if not decision.allowed:
            raise MemoryWriteRejected(decision)

    async def write(
        self,
        repository: MemoryRepository,
        candidate: MemoryCandidate,
    ) -> MemoryRecord:
        self.require(candidate)
        return await repository.upsert(candidate)

    def _contains_sensitive_value(self, value: Any, *, key: str | None = None) -> bool:
        if key is not None and key.casefold() in _SENSITIVE_KEYS and value not in (None, ""):
            return True
        if isinstance(value, Mapping):
            return any(
                self._contains_sensitive_value(item, key=str(item_key))
                for item_key, item in value.items()
            )
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(self._contains_sensitive_value(item) for item in value)
        return False

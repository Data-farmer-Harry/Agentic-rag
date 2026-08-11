from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.models import (
    GraphEntityCandidate,
    GraphExtractionBatch,
    GraphRelationCandidate,
    KnowledgeChunk,
    KnowledgeDocument,
)
from app.graph.graph_identity import (
    entity_candidate_id,
    extraction_batch_id,
    normalized_entity_key,
    relation_candidate_id,
)

_HEADING_PATTERN = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.{2,180}?)\s*$")
_BACKTICK_PATTERN = re.compile(r"(?<!`)`([^`\n]{2,120})`(?!`)")
_IDENTIFIER_PATTERN = re.compile(
    r"(?<![\w-])(?:[A-Z]{2,}[A-Z0-9_-]*|[A-Za-z][A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+)(?![\w-])"
)
_ENGLISH_RELATION_PATTERN = re.compile(
    r"(?P<source>(?:the[ \t]+)?[A-Z][A-Za-z0-9+.#/_-]*"
    r"(?:[ \t]+[A-Za-z0-9+.#/_-]+){0,6}?)[ \t]+"
    r"(?P<verb>requires?|uses?|depends\s+on|supports?|contains?|integrates\s+with|"
    r"extends?|implements?|orchestrates?|delegates\s+to|connects\s+to)\s+"
    r"(?P<target>[^.;:\n]{2,180})",
    re.IGNORECASE,
)
_CHINESE_RELATION_PATTERN = re.compile(
    r"(?P<source>[^，。；：\n]{2,80}?)\s*"
    r"(?P<verb>依赖|需要|使用|支持|包含|连接|扩展|实现|编排|委托给)\s*"
    r"(?P<target>[^，。；\n]{2,140})"
)
_RELATION_TYPES = {
    "require": "requires",
    "requires": "requires",
    "use": "uses",
    "uses": "uses",
    "depend on": "depends_on",
    "depends on": "depends_on",
    "support": "supports",
    "supports": "supports",
    "contain": "contains",
    "contains": "contains",
    "integrate with": "integrates_with",
    "integrates with": "integrates_with",
    "extend": "extends",
    "extends": "extends",
    "implement": "implements",
    "implements": "implements",
    "orchestrate": "orchestrates",
    "orchestrates": "orchestrates",
    "delegate to": "delegates_to",
    "delegates to": "delegates_to",
    "connect to": "connects_to",
    "connects to": "connects_to",
    "依赖": "depends_on",
    "需要": "requires",
    "使用": "uses",
    "支持": "supports",
    "包含": "contains",
    "连接": "connects_to",
    "扩展": "extends",
    "实现": "implements",
    "编排": "orchestrates",
    "委托给": "delegates_to",
}
_LEADING_MARKUP = re.compile(r"^(?:[-*+>]\s+|\d+[.)]\s+|#{1,6}\s+)+")
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)
_TARGET_TRAILER = re.compile(r"\s+(?:before|after|when|if|unless|while)\s+.*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _EntityMention:
    name: str
    entity_type: str
    chunk_id: UUID
    confidence: float
    rationale: str


@dataclass(frozen=True, slots=True)
class _RelationMention:
    source: str
    target: str
    relation_type: str
    chunk_id: UUID
    confidence: float
    rationale: str


class RuleBasedEntityRelationExtractor:
    """Conservative offline candidate extractor; it never promotes graph facts."""

    revision = "rule-entity-relation-v1"

    def __init__(
        self,
        *,
        max_entities: int = 500,
        max_relations: int = 500,
    ) -> None:
        if not 1 <= max_entities <= 5_000:
            raise ValueError("max_entities must be between 1 and 5000")
        if not 1 <= max_relations <= 5_000:
            raise ValueError("max_relations must be between 1 and 5000")
        self._max_entities = max_entities
        self._max_relations = max_relations

    async def extract(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        *,
        domain_pack: str = "general",
    ) -> GraphExtractionBatch:
        if any(
            chunk.document_id != document.document_id
            or chunk.tenant_id != document.tenant_id
            or chunk.project_id != document.project_id
            for chunk in chunks
        ):
            raise ValueError("All extraction chunks must belong to the document scope")

        entity_mentions: list[_EntityMention] = []
        relation_mentions: list[_RelationMention] = []
        for chunk in chunks:
            entity_mentions.extend(self._entity_mentions(chunk))
            relation_mentions.extend(self._relation_mentions(chunk))

        entities = self._build_entities(document, entity_mentions)
        entity_lookup = {
            _entity_key(item.canonical_name): item for item in entities
        }
        for mention in relation_mentions:
            for name in (mention.source, mention.target):
                key = _entity_key(name)
                if key in entity_lookup or len(entities) >= self._max_entities:
                    continue
                candidate = self._entity_candidate(
                    document,
                    _EntityMention(
                        name=name,
                        entity_type=_infer_entity_type(name),
                        chunk_id=mention.chunk_id,
                        confidence=mention.confidence,
                        rationale="Entity participating in an explicit relation candidate.",
                    ),
                )
                entities.append(candidate)
                entity_lookup[key] = candidate

        relations = self._build_relations(
            document,
            relation_mentions,
            entity_lookup,
        )
        batch_id = extraction_batch_id(
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            document_id=document.document_id,
            domain_pack=domain_pack,
            extractor_revision=self.revision,
        )
        return GraphExtractionBatch(
            batch_id=batch_id,
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            domain_pack=domain_pack,
            extractor_revision=self.revision,
            entities=sorted(entities, key=lambda item: str(item.candidate_id)),
            relations=sorted(relations, key=lambda item: str(item.candidate_id)),
        )

    def _entity_mentions(self, chunk: KnowledgeChunk) -> list[_EntityMention]:
        mentions: list[_EntityMention] = []
        for match in _HEADING_PATTERN.finditer(chunk.text):
            if name := _clean_phrase(match.group(1)):
                mentions.append(
                    _EntityMention(
                        name=name,
                        entity_type="Concept",
                        chunk_id=chunk.chunk_id,
                        confidence=0.84,
                        rationale="Markdown heading extracted as a concept candidate.",
                    )
                )
        for match in _BACKTICK_PATTERN.finditer(chunk.text):
            if name := _clean_phrase(match.group(1)):
                mentions.append(
                    _EntityMention(
                        name=name,
                        entity_type="SoftwareSymbol",
                        chunk_id=chunk.chunk_id,
                        confidence=0.8,
                        rationale="Inline code span extracted as a software symbol candidate.",
                    )
                )
        for match in _IDENTIFIER_PATTERN.finditer(chunk.text):
            if name := _clean_phrase(match.group(0)):
                mentions.append(
                    _EntityMention(
                        name=name,
                        entity_type=_infer_entity_type(name),
                        chunk_id=chunk.chunk_id,
                        confidence=0.76,
                        rationale="Stable identifier pattern extracted from source text.",
                    )
                )
        return mentions

    def _relation_mentions(self, chunk: KnowledgeChunk) -> list[_RelationMention]:
        mentions: list[_RelationMention] = []
        for pattern in (_ENGLISH_RELATION_PATTERN, _CHINESE_RELATION_PATTERN):
            for match in pattern.finditer(chunk.text):
                source = _clean_phrase(match.group("source"))
                target = _clean_phrase(_TARGET_TRAILER.sub("", match.group("target")))
                relation_type = _RELATION_TYPES.get(
                    " ".join(match.group("verb").casefold().split())
                )
                if (
                    source is None
                    or target is None
                    or relation_type is None
                    or _entity_key(source) == _entity_key(target)
                ):
                    continue
                mentions.append(
                    _RelationMention(
                        source=source,
                        target=target,
                        relation_type=relation_type,
                        chunk_id=chunk.chunk_id,
                        confidence=0.87,
                        rationale="Explicit source-text relation pattern; requires review.",
                    )
                )
        return mentions

    def _build_entities(
        self,
        document: KnowledgeDocument,
        mentions: Sequence[_EntityMention],
    ) -> list[GraphEntityCandidate]:
        entities: dict[str, GraphEntityCandidate] = {}
        for mention in mentions:
            key = _entity_key(mention.name)
            existing = entities.get(key)
            if existing is None:
                if len(entities) >= self._max_entities:
                    break
                entities[key] = self._entity_candidate(document, mention)
                continue
            chunk_ids = sorted(
                {*existing.source_chunk_ids, mention.chunk_id},
                key=str,
            )
            aliases = sorted(
                {*existing.aliases, mention.name} - {existing.canonical_name},
                key=str.casefold,
            )[:20]
            entities[key] = existing.model_copy(
                update={
                    "aliases": aliases,
                    "source_chunk_ids": chunk_ids,
                    "confidence": max(existing.confidence, mention.confidence),
                }
            )
        return list(entities.values())

    def _build_relations(
        self,
        document: KnowledgeDocument,
        mentions: Sequence[_RelationMention],
        entities: dict[str, GraphEntityCandidate],
    ) -> list[GraphRelationCandidate]:
        relations: dict[tuple[UUID, str, UUID], GraphRelationCandidate] = {}
        for mention in mentions:
            source = entities.get(_entity_key(mention.source))
            target = entities.get(_entity_key(mention.target))
            if source is None or target is None:
                continue
            key = (source.candidate_id, mention.relation_type, target.candidate_id)
            existing = relations.get(key)
            if existing is not None:
                relations[key] = existing.model_copy(
                    update={
                        "source_chunk_ids": sorted(
                            {*existing.source_chunk_ids, mention.chunk_id},
                            key=str,
                        ),
                        "confidence": max(existing.confidence, mention.confidence),
                    }
                )
                continue
            if len(relations) >= self._max_relations:
                break
            candidate_id = relation_candidate_id(
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                document_id=document.document_id,
                source_candidate_id=source.candidate_id,
                relation_type=mention.relation_type,
                target_candidate_id=target.candidate_id,
            )
            relations[key] = GraphRelationCandidate(
                candidate_id=candidate_id,
                document_id=document.document_id,
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                source_candidate_id=source.candidate_id,
                target_candidate_id=target.candidate_id,
                source_name=source.canonical_name,
                target_name=target.canonical_name,
                relation_type=mention.relation_type,
                source_chunk_ids=[mention.chunk_id],
                confidence=mention.confidence,
                extractor_revision=self.revision,
                rationale=mention.rationale,
            )
        return list(relations.values())

    def _entity_candidate(
        self,
        document: KnowledgeDocument,
        mention: _EntityMention,
    ) -> GraphEntityCandidate:
        candidate_id = entity_candidate_id(
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            document_id=document.document_id,
            entity_type=mention.entity_type,
            canonical_name=mention.name,
        )
        return GraphEntityCandidate(
            candidate_id=candidate_id,
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            canonical_name=mention.name,
            entity_type=mention.entity_type,
            source_chunk_ids=[mention.chunk_id],
            confidence=mention.confidence,
            extractor_revision=self.revision,
            rationale=mention.rationale,
        )


def _clean_phrase(value: str) -> str | None:
    cleaned = " ".join(value.replace("`", "").split())
    cleaned = _LEADING_MARKUP.sub("", cleaned)
    cleaned = _LEADING_ARTICLE.sub("", cleaned)
    cleaned = cleaned.strip(" \t\r\n,.;:!?()[]{}<>\"'，。；：！？（）【】")
    if not 2 <= len(cleaned) <= 300:
        return None
    return cleaned


def _entity_key(value: str) -> str:
    return normalized_entity_key(value)


def _infer_entity_type(value: str) -> str:
    if any(character in value for character in (".", "_", "/", "::")):
        return "SoftwareSymbol"
    if re.search(r"[A-Z]{2,}|\d", value):
        return "Identifier"
    return "Concept"

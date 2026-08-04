from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from neo4j import AsyncDriver, Query, RoutingControl

from app.domain.models import (
    EntityResolutionCandidate,
    EvidenceRef,
    GraphEntityCandidate,
    GraphEntityMatch,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphExtractionBatch,
    GraphNode,
    GraphPath,
    GraphRelationCandidate,
    GraphRelationship,
    GraphSearchRequest,
    GraphSearchResult,
    KnowledgeChunk,
    KnowledgeDocument,
    Provenance,
    RunContext,
)
from app.knowledge.visibility import document_visibility_metadata

_SCHEMA_QUERIES = (
    """
CREATE CONSTRAINT hermesgraph_document_scope IF NOT EXISTS
FOR (node:Document)
REQUIRE (node.tenant_id, node.project_id, node.node_id) IS UNIQUE
""",
    """
CREATE CONSTRAINT hermesgraph_chunk_scope IF NOT EXISTS
FOR (node:Chunk)
REQUIRE (node.tenant_id, node.project_id, node.node_id) IS UNIQUE
""",
    """
CREATE INDEX hermesgraph_document_name IF NOT EXISTS
FOR (node:Document) ON (node.tenant_id, node.project_id, node.name)
""",
    """
CREATE INDEX hermesgraph_chunk_id IF NOT EXISTS
FOR (node:Chunk) ON (node.tenant_id, node.project_id, node.chunk_id)
""",
    """
CREATE CONSTRAINT hermesgraph_entity_scope IF NOT EXISTS
FOR (node:Entity)
REQUIRE (node.tenant_id, node.project_id, node.node_id) IS UNIQUE
""",
    """
CREATE INDEX hermesgraph_entity_name IF NOT EXISTS
FOR (node:Entity) ON (node.tenant_id, node.project_id, node.name)
""",
)

_INDEX_DOCUMENT_QUERY = """
MERGE (document:Document {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: $document_id
})
SET document.document_id = $document_id,
    document.label = 'Document',
    document.name = $title,
    document.filename = $filename,
    document.media_type = $media_type,
    document.modality = $document_modality,
    document.content_hash = $document_content_hash,
    document.source_type = $source_type,
    document.source_id = $source_id,
    document.source_revision = $source_revision,
    document.canonical_uri = $canonical_uri,
    document.license_uri = $license_uri,
    document.privacy = $privacy,
    document.source_status = $source_status,
    document.user_id = $user_id,
    document.knowledge_layer = $knowledge_layer,
    document.trust = $trust,
    document.status = 'active',
    document.updated_at = $updated_at
WITH document
UNWIND $chunks AS item
MERGE (chunk:Chunk {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: item.node_id
})
SET chunk.chunk_id = item.chunk_id,
    chunk.document_id = $document_id,
    chunk.label = 'Chunk',
    chunk.name = item.name,
    chunk.text = item.text,
    chunk.title = $title,
    chunk.source_type = item.source_type,
    chunk.source_id = item.source_id,
    chunk.source_revision = item.source_revision,
    chunk.canonical_uri = item.canonical_uri,
    chunk.license_uri = item.license_uri,
    chunk.privacy = item.privacy,
    chunk.source_status = $source_status,
    chunk.user_id = $user_id,
    chunk.knowledge_layer = $knowledge_layer,
    chunk.content_hash = item.content_hash,
    chunk.trust = item.trust,
    chunk.page_number = item.page_number,
    chunk.chunk_index = item.chunk_index,
    chunk.modality = item.modality,
    chunk.visual_kind = item.visual_kind,
    chunk.visual_region_id = item.visual_region_id,
    chunk.visual_category = item.visual_category,
    chunk.visual_bbox = item.visual_bbox,
    chunk.vision_confidence = item.vision_confidence,
    chunk.document_ir_schema = item.document_ir_schema,
    chunk.parser_revision = item.parser_revision,
    chunk.chunker_revision = item.chunker_revision,
    chunk.chunk_strategy = item.chunk_strategy,
    chunk.chunk_level = item.chunk_level,
    chunk.parent_section_id = item.parent_section_id,
    chunk.section_id = item.section_id,
    chunk.heading_path = item.heading_path,
    chunk.block_ids = item.block_ids,
    chunk.block_kinds = item.block_kinds,
    chunk.page_start = item.page_start,
    chunk.page_end = item.page_end,
    chunk.extraction_methods = item.extraction_methods,
    chunk.ocr_confidence_min = item.ocr_confidence_min,
    chunk.token_count = item.token_count,
    chunk.status = 'active',
    chunk.updated_at = $updated_at
MERGE (document)-[relationship:HAS_CHUNK {
  relationship_id: item.relationship_id
}]->(chunk)
SET relationship.tenant_id = $tenant_id,
    relationship.project_id = $project_id,
    relationship.relation_type = 'HAS_CHUNK',
    relationship.source_node_id = $document_id,
    relationship.target_node_id = item.node_id,
    relationship.source_chunk_ids = [item.chunk_id],
    relationship.confidence = 1.0,
    relationship.extractor_version = 'structural-v1',
    relationship.status = 'active',
    relationship.updated_at = $updated_at
"""

_ARCHIVE_STALE_DOCUMENT_CHUNKS_QUERY = """
MATCH (document:Document {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: $document_id
})-[relationship:HAS_CHUNK]->(chunk:Chunk)
WHERE NOT chunk.node_id IN $active_chunk_ids
SET relationship.status = 'archived',
    chunk.status = 'archived',
    relationship.updated_at = $updated_at,
    chunk.updated_at = $updated_at
RETURN count(chunk) AS archived_count
"""

_ARCHIVE_DOCUMENT_QUERY = """
MATCH (document:Document {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: $document_id
})
OPTIONAL MATCH (document)-[relationship:HAS_CHUNK]->(chunk:Chunk)
SET document.status = 'archived',
    relationship.status = 'archived',
    chunk.status = 'archived',
    document.updated_at = $updated_at,
    relationship.updated_at = $updated_at,
    chunk.updated_at = $updated_at
"""

_INDEX_ENTITY_CANDIDATES_QUERY = """
UNWIND $entities AS item
MERGE (entity:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: item.node_id
})
SET entity.candidate_id = item.candidate_id,
    entity.document_id = $document_id,
    entity.label = item.entity_type,
    entity.name = item.canonical_name,
    entity.aliases = item.aliases,
    entity.source_chunk_ids = item.source_chunk_ids,
    entity.confidence = item.confidence,
    entity.extractor_version = item.extractor_version,
    entity.candidate_status = item.candidate_status,
    entity.status = item.graph_status,
    entity.reviewed_by = item.reviewed_by,
    entity.reviewed_at = item.reviewed_at,
    entity.updated_at = $updated_at
"""

_ARCHIVE_STALE_PENDING_SEMANTIC_QUERY = """
OPTIONAL MATCH ()-[relationship:SEMANTIC_RELATION {
  tenant_id: $tenant_id,
  project_id: $project_id,
  document_id: $document_id
}]->()
WHERE relationship.candidate_status = 'pending'
  AND NOT relationship.relationship_id IN $active_relation_ids
FOREACH (_ IN CASE WHEN relationship IS NULL THEN [] ELSE [1] END |
  SET relationship.candidate_status = 'archived',
      relationship.status = 'archived',
      relationship.updated_at = $updated_at
)
WITH count(relationship) AS archived_relationships
OPTIONAL MATCH (entity:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  document_id: $document_id
})
WHERE entity.candidate_status = 'pending'
  AND NOT entity.node_id IN $active_entity_ids
FOREACH (_ IN CASE WHEN entity IS NULL THEN [] ELSE [1] END |
  SET entity.candidate_status = 'archived',
      entity.status = 'archived',
      entity.updated_at = $updated_at
)
RETURN archived_relationships, count(entity) AS archived_entities
"""

_INDEX_RELATION_CANDIDATES_QUERY = """
UNWIND $relations AS item
MATCH (source:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: item.source_node_id
})
MATCH (target:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: item.target_node_id
})
MERGE (source)-[relationship:SEMANTIC_RELATION {
  relationship_id: item.relationship_id
}]->(target)
SET relationship.candidate_id = item.candidate_id,
    relationship.document_id = $document_id,
    relationship.tenant_id = $tenant_id,
    relationship.project_id = $project_id,
    relationship.relation_type = item.relation_type,
    relationship.source_node_id = item.source_node_id,
    relationship.target_node_id = item.target_node_id,
    relationship.source_chunk_ids = item.source_chunk_ids,
    relationship.confidence = item.confidence,
    relationship.extractor_version = item.extractor_version,
    relationship.candidate_status = item.candidate_status,
    relationship.status = item.graph_status,
    relationship.reviewed_by = item.reviewed_by,
    relationship.reviewed_at = item.reviewed_at,
    relationship.updated_at = $updated_at
"""

_INDEX_ENTITY_RESOLUTIONS_QUERY = """
UNWIND $resolutions AS item
MATCH (left:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: item.left_node_id
})
MATCH (right:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  node_id: item.right_node_id
})
MERGE (left)-[relationship:ENTITY_RESOLUTION {
  relationship_id: item.relationship_id
}]->(right)
SET relationship.candidate_id = item.candidate_id,
    relationship.tenant_id = $tenant_id,
    relationship.project_id = $project_id,
    relationship.relation_type = 'same_as',
    relationship.source_node_id = item.left_node_id,
    relationship.target_node_id = item.right_node_id,
    relationship.canonical_name = item.canonical_name,
    relationship.match_strategy = item.match_strategy,
    relationship.source_chunk_ids = item.source_chunk_ids,
    relationship.confidence = item.confidence,
    relationship.resolver_version = item.resolver_version,
    relationship.candidate_status = item.candidate_status,
    relationship.status = item.graph_status,
    relationship.reviewed_by = item.reviewed_by,
    relationship.reviewed_at = item.reviewed_at,
    relationship.updated_at = $updated_at
"""

_ARCHIVE_SEMANTIC_DOCUMENT_QUERY = """
MATCH (entity:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  document_id: $document_id
})
OPTIONAL MATCH (entity)-[relationship:SEMANTIC_RELATION]-()
WHERE relationship.tenant_id = $tenant_id
  AND relationship.project_id = $project_id
  AND relationship.document_id = $document_id
OPTIONAL MATCH (entity)-[resolution:ENTITY_RESOLUTION]-()
WHERE resolution.tenant_id = $tenant_id
  AND resolution.project_id = $project_id
SET entity.candidate_status = 'archived',
    entity.status = 'archived',
    relationship.candidate_status = 'archived',
    relationship.status = 'archived',
    resolution.candidate_status = 'archived',
    resolution.status = 'archived',
    entity.updated_at = $updated_at,
    relationship.updated_at = $updated_at,
    resolution.updated_at = $updated_at
"""

_SET_ENTITY_STATUS_QUERY = """
MATCH (entity:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  candidate_id: $candidate_id
})
SET entity.candidate_status = $candidate_status,
    entity.status = $graph_status,
    entity.reviewed_by = $reviewed_by,
    entity.reviewed_at = $reviewed_at,
    entity.updated_at = $updated_at
"""

_SET_RELATION_STATUS_QUERY = """
MATCH ()-[relationship:SEMANTIC_RELATION {
  tenant_id: $tenant_id,
  project_id: $project_id,
  candidate_id: $candidate_id
}]->()
SET relationship.candidate_status = $candidate_status,
    relationship.status = $graph_status,
    relationship.reviewed_by = $reviewed_by,
    relationship.reviewed_at = $reviewed_at,
    relationship.updated_at = $updated_at
"""

_SET_RESOLUTION_STATUS_QUERY = """
MATCH ()-[relationship:ENTITY_RESOLUTION {
  tenant_id: $tenant_id,
  project_id: $project_id,
  candidate_id: $candidate_id
}]->()
SET relationship.candidate_status = $candidate_status,
    relationship.status = $graph_status,
    relationship.reviewed_by = $reviewed_by,
    relationship.reviewed_at = $reviewed_at,
    relationship.updated_at = $updated_at
"""

_PRUNE_PENDING_ENTITY_RESOLUTIONS_QUERY = """
MATCH ()-[relationship:ENTITY_RESOLUTION]->()
WHERE relationship.tenant_id = $tenant_id
  AND relationship.project_id = $project_id
  AND relationship.candidate_status = 'pending'
  AND NOT relationship.candidate_id IN $kept_candidate_ids
DELETE relationship
RETURN count(relationship) AS deleted_count
"""

_RECONCILE_STALE_PENDING_RELATIONS_QUERY = """
MATCH ()-[candidate:SEMANTIC_RELATION {
  tenant_id: $tenant_id,
  project_id: $project_id,
  candidate_status: 'pending'
}]->()
WHERE size(coalesce(candidate.source_chunk_ids, [])) = 0
  OR any(source_chunk_id IN candidate.source_chunk_ids WHERE NOT EXISTS {
    MATCH (chunk:Chunk {
      tenant_id: $tenant_id,
      project_id: $project_id,
      chunk_id: source_chunk_id,
      status: 'active'
    })
  })
FOREACH (_ IN CASE WHEN $dry_run THEN [] ELSE [1] END |
  SET candidate.candidate_status = 'archived',
      candidate.status = 'archived',
      candidate.updated_at = $updated_at
)
RETURN count(candidate) AS stale_count
"""

_RECONCILE_STALE_PENDING_ENTITIES_QUERY = """
MATCH (candidate:Entity {
  tenant_id: $tenant_id,
  project_id: $project_id,
  candidate_status: 'pending'
})
WHERE size(coalesce(candidate.source_chunk_ids, [])) = 0
  OR any(source_chunk_id IN candidate.source_chunk_ids WHERE NOT EXISTS {
    MATCH (chunk:Chunk {
      tenant_id: $tenant_id,
      project_id: $project_id,
      chunk_id: source_chunk_id,
      status: 'active'
    })
  })
FOREACH (_ IN CASE WHEN $dry_run THEN [] ELSE [1] END |
  SET candidate.candidate_status = 'archived',
      candidate.status = 'archived',
      candidate.updated_at = $updated_at
)
RETURN count(candidate) AS stale_count
"""

_RECONCILE_STALE_PENDING_RESOLUTIONS_QUERY = """
MATCH ()-[candidate:ENTITY_RESOLUTION {
  tenant_id: $tenant_id,
  project_id: $project_id,
  candidate_status: 'pending'
}]->()
WHERE size(coalesce(candidate.source_chunk_ids, [])) = 0
  OR any(source_chunk_id IN candidate.source_chunk_ids WHERE NOT EXISTS {
    MATCH (chunk:Chunk {
      tenant_id: $tenant_id,
      project_id: $project_id,
      chunk_id: source_chunk_id,
      status: 'active'
    })
  })
FOREACH (_ IN CASE WHEN $dry_run THEN [] ELSE [1] END |
  SET candidate.candidate_status = 'archived',
      candidate.status = 'archived',
      candidate.updated_at = $updated_at
)
RETURN count(candidate) AS stale_count
"""

_NODE_PROJECTION = """
[node IN nodes(path) | {
  node_id: coalesce(node.node_id, elementId(node)),
  tenant_id: node.tenant_id,
  project_id: node.project_id,
  label: coalesce(node.label, head(labels(node))),
  name: node.name,
  properties: {
    aliases: coalesce(node.aliases, []),
    confidence: node.confidence,
    extractor_version: node.extractor_version,
    candidate_status: node.candidate_status,
    reviewed_by: node.reviewed_by,
    reviewed_at: node.reviewed_at
  },
  provenance: []
}] AS nodes
"""

_RELATIONSHIP_PROJECTION = """
[rel IN relationships(path) | {
  relationship_id: coalesce(rel.relationship_id, elementId(rel)),
  tenant_id: rel.tenant_id,
  project_id: rel.project_id,
  relation_type: coalesce(rel.relation_type, type(rel)),
  source_node_id: coalesce(rel.source_node_id, startNode(rel).node_id, elementId(startNode(rel))),
  target_node_id: coalesce(rel.target_node_id, endNode(rel).node_id, elementId(endNode(rel))),
  properties: {
    confidence: rel.confidence,
    extractor_version: rel.extractor_version,
    candidate_status: rel.candidate_status,
    reviewed_by: rel.reviewed_by,
    reviewed_at: rel.reviewed_at
  },
  evidence: [chunk IN evidence_chunks
    WHERE chunk.chunk_id IN coalesce(rel.source_chunk_ids, []) | {
      text: left(chunk.text, 6000),
      title: coalesce(chunk.title, 'Graph evidence'),
      score: 1.0,
      provenance: {
        source_type: coalesce(chunk.source_type, 'graph_chunk'),
        source_id: coalesce(chunk.source_id, chunk.chunk_id),
        content_hash: chunk.content_hash,
        locator: {page: chunk.page_number},
        trust: coalesce(chunk.trust, 'user_asserted')
      },
      metadata: {
        chunk_id: chunk.chunk_id,
        document_id: chunk.document_id,
        user_id: chunk.user_id,
        knowledge_layer: chunk.knowledge_layer,
        source_status: coalesce(chunk.source_status, 'active')
      }
    }]
}] AS relationships
"""

_EVIDENCE_JOIN = """
WITH path
OPTIONAL MATCH (chunk:Chunk)
WHERE chunk.tenant_id = $tenant_id
  AND chunk.project_id = $project_id
  AND coalesce(chunk.status, 'active') = 'active'
  AND chunk.knowledge_layer IN $knowledge_layers
  AND (chunk.knowledge_layer <> 'personal' OR chunk.user_id = $user_id)
  AND chunk.chunk_id IN reduce(
    chunk_ids = [], edge IN relationships(path) |
    chunk_ids + coalesce(edge.source_chunk_ids, [])
  )
WITH path, collect(DISTINCT chunk) AS evidence_chunks
"""


def _neighbors_query() -> str:
    return f"""
MATCH path=(start:Entity)-[rel:SEMANTIC_RELATION]-(neighbor:Entity)
WHERE start.tenant_id = $tenant_id
  AND start.project_id = $project_id
  AND neighbor.tenant_id = $tenant_id
  AND neighbor.project_id = $project_id
  AND rel.tenant_id = $tenant_id
  AND rel.project_id = $project_id
  AND coalesce(start.status, 'active') = 'active'
  AND coalesce(neighbor.status, 'active') = 'active'
  AND coalesce(rel.status, 'active') = 'active'
  AND any(entity IN $entities WHERE toLower(start.name) CONTAINS entity)
{_EVIDENCE_JOIN}
RETURN {_NODE_PROJECTION}, {_RELATIONSHIP_PROJECTION}
LIMIT $limit
"""


def _paths_query(max_hops: int) -> str:
    return f"""
MATCH path=(start:Entity)-[:SEMANTIC_RELATION*1..{max_hops}]-(target:Entity)
WHERE start.tenant_id = $tenant_id
  AND start.project_id = $project_id
  AND any(entity IN $entities WHERE toLower(start.name) CONTAINS entity)
  AND all(node IN nodes(path)
    WHERE node.tenant_id = $tenant_id
      AND node.project_id = $project_id
      AND coalesce(node.status, 'active') = 'active')
  AND all(rel IN relationships(path)
    WHERE rel.tenant_id = $tenant_id
      AND rel.project_id = $project_id
      AND coalesce(rel.status, 'active') = 'active')
{_EVIDENCE_JOIN}
RETURN {_NODE_PROJECTION}, {_RELATIONSHIP_PROJECTION}
LIMIT $limit
"""


def _conflicts_query() -> str:
    return f"""
MATCH path=(start:Entity)-[rel:SEMANTIC_RELATION]-(target:Entity)
WHERE start.tenant_id = $tenant_id
  AND start.project_id = $project_id
  AND target.tenant_id = $tenant_id
  AND target.project_id = $project_id
  AND rel.tenant_id = $tenant_id
  AND rel.project_id = $project_id
  AND coalesce(start.status, 'active') = 'active'
  AND coalesce(target.status, 'active') = 'active'
  AND coalesce(rel.status, 'active') = 'active'
  AND any(entity IN $entities WHERE toLower(start.name) CONTAINS entity)
  AND (type(rel) IN ['CONFLICTS_WITH', 'CONTRADICTS', 'DISPUTES']
    OR toLower(coalesce(rel.relation_type, '')) IN
      ['conflicts_with', 'contradicts', 'disputes'])
{_EVIDENCE_JOIN}
RETURN {_NODE_PROJECTION}, {_RELATIONSHIP_PROJECTION}
LIMIT $limit
"""


_TEMPLATES = {
    "neighbors": _neighbors_query(),
    "conflicts": _conflicts_query(),
}
_PATH_TEMPLATES = {hops: _paths_query(hops) for hops in (1, 2, 3)}

_ENTITY_RESOLUTION_QUERY = """
MATCH (entity:Entity)
WHERE entity.tenant_id = $tenant_id
  AND entity.project_id = $project_id
  AND coalesce(entity.status, 'active') = 'active'
  AND (size($entity_types) = 0 OR toLower(entity.label) IN $entity_types)
UNWIND $mentions AS matched_text
WITH entity, matched_text
WHERE toLower(entity.name) CONTAINS matched_text
  OR matched_text CONTAINS toLower(entity.name)
  OR any(alias IN coalesce(entity.aliases, []) WHERE
    toLower(alias) CONTAINS matched_text OR matched_text CONTAINS toLower(alias))
WITH entity, matched_text,
  CASE
    WHEN toLower(entity.name) = matched_text THEN 1.0
    WHEN any(alias IN coalesce(entity.aliases, []) WHERE toLower(alias) = matched_text)
      THEN 0.96
    WHEN matched_text CONTAINS toLower(entity.name) THEN 0.90
    WHEN any(alias IN coalesce(entity.aliases, []) WHERE
      matched_text CONTAINS toLower(alias)) THEN 0.88
    WHEN toLower(entity.name) CONTAINS matched_text THEN 0.78
    ELSE 0.74
  END AS score,
  CASE
    WHEN toLower(entity.name) CONTAINS matched_text
      OR matched_text CONTAINS toLower(entity.name)
      THEN 'canonical_name'
    ELSE 'alias'
  END AS matched_field
WHERE score >= $min_score
ORDER BY entity.node_id, score DESC, matched_text
WITH entity, head(collect({
  matched_text: matched_text,
  matched_field: matched_field,
  score: score
})) AS best_match
WITH entity,
  best_match.matched_text AS matched_text,
  best_match.matched_field AS matched_field,
  best_match.score AS score
OPTIONAL MATCH (chunk:Chunk)
WHERE chunk.tenant_id = $tenant_id
  AND chunk.project_id = $project_id
  AND coalesce(chunk.status, 'active') = 'active'
  AND chunk.chunk_id IN coalesce(entity.source_chunk_ids, [])
  AND chunk.knowledge_layer IN $knowledge_layers
  AND (chunk.knowledge_layer <> 'personal' OR chunk.user_id = $user_id)
WITH entity, matched_text, matched_field, score,
  [chunk IN collect(DISTINCT chunk) WHERE chunk.text IS NOT NULL | {
    text: left(chunk.text, 6000),
    title: coalesce(chunk.title, 'Entity evidence'),
    score: score,
    provenance: {
      source_type: coalesce(chunk.source_type, 'graph_chunk'),
      source_id: coalesce(chunk.source_id, chunk.chunk_id),
      content_hash: chunk.content_hash,
      locator: {page: chunk.page_number},
      trust: coalesce(chunk.trust, 'user_asserted')
    },
    metadata: {
      chunk_id: chunk.chunk_id,
      document_id: chunk.document_id,
      user_id: chunk.user_id,
      knowledge_layer: chunk.knowledge_layer,
      source_status: coalesce(chunk.source_status, 'active')
    }
  }] AS evidence
WHERE size(evidence) > 0
RETURN {
  node_id: coalesce(entity.node_id, elementId(entity)),
  tenant_id: entity.tenant_id,
  project_id: entity.project_id,
  label: coalesce(entity.label, head(labels(entity))),
  name: entity.name,
  properties: {
    aliases: coalesce(entity.aliases, []),
    confidence: entity.confidence,
    extractor_version: entity.extractor_version,
    candidate_status: entity.candidate_status,
    reviewed_by: entity.reviewed_by,
    reviewed_at: entity.reviewed_at
  },
  provenance: []
} AS node,
matched_text,
matched_field,
score,
evidence
ORDER BY score DESC, toLower(entity.name), entity.node_id
LIMIT $limit
"""


class Neo4jEvidenceGraph:
    """Read-only, allowlisted Neo4j traversal with application-level scope checks."""

    def __init__(
        self,
        driver: AsyncDriver,
        *,
        database: str = "neo4j",
        timeout_seconds: float = 20.0,
    ) -> None:
        if not database:
            raise ValueError("Neo4j database is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._driver = driver
        self._database = database
        self._timeout_seconds = timeout_seconds
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            for cypher in _SCHEMA_QUERIES:
                await self._driver.execute_query(
                    Query(cypher, timeout=self._timeout_seconds),
                    routing_=RoutingControl.WRITE,
                    database_=self._database,
                )
            self._schema_ready = True

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        if not chunks:
            raise ValueError("At least one chunk is required for graph indexing")
        if any(
            chunk.document_id != document.document_id
            or chunk.tenant_id != document.tenant_id
            or chunk.project_id != document.project_id
            for chunk in chunks
        ):
            raise ValueError("All graph chunks must belong to the document scope")
        await self.ensure_schema()
        common_parameters = {
            "tenant_id": document.tenant_id,
            "project_id": document.project_id,
            "document_id": str(document.document_id),
            "title": document.title,
            "filename": document.filename,
            "media_type": document.media_type,
            "document_modality": document.metadata.get("modality", "text"),
            "document_content_hash": document.content_hash,
            "source_type": document.source.source_type,
            "source_id": document.source.source_id or str(document.document_id),
            "source_revision": document.source.source_revision,
            "canonical_uri": document.source.canonical_uri,
            "license_uri": document.source.license_uri,
            "privacy": document.source.privacy,
            "source_status": document.source.source_status,
            **document_visibility_metadata(document),
            "trust": document.source.trust.value,
            "updated_at": document.updated_at.isoformat(),
        }
        for start in range(0, len(chunks), 200):
            batch = chunks[start : start + 200]
            parameters = {
                **common_parameters,
                "chunks": [self._chunk_payload(document, chunk) for chunk in batch],
            }
            await self._driver.execute_query(
                Query(_INDEX_DOCUMENT_QUERY, timeout=self._timeout_seconds),
                parameters_=parameters,
                routing_=RoutingControl.WRITE,
                database_=self._database,
            )
        await self._driver.execute_query(
            Query(
                _ARCHIVE_STALE_DOCUMENT_CHUNKS_QUERY,
                timeout=self._timeout_seconds,
            ),
            parameters_={
                "tenant_id": document.tenant_id,
                "project_id": document.project_id,
                "document_id": str(document.document_id),
                "active_chunk_ids": [str(chunk.chunk_id) for chunk in chunks],
                "updated_at": document.updated_at.isoformat(),
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        await self.ensure_schema()
        await self._driver.execute_query(
            Query(_ARCHIVE_DOCUMENT_QUERY, timeout=self._timeout_seconds),
            parameters_={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "document_id": str(document_id),
                "updated_at": datetime.now(UTC).isoformat(),
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )
        await self._driver.execute_query(
            Query(_ARCHIVE_SEMANTIC_DOCUMENT_QUERY, timeout=self._timeout_seconds),
            parameters_={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "document_id": str(document_id),
                "updated_at": datetime.now(UTC).isoformat(),
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def index_extraction(self, batch: GraphExtractionBatch) -> None:
        entity_ids = {item.candidate_id for item in batch.entities}
        if any(
            item.document_id != batch.document_id
            or item.tenant_id != batch.tenant_id
            or item.project_id != batch.project_id
            for item in batch.entities
        ) or any(
            item.document_id != batch.document_id
            or item.tenant_id != batch.tenant_id
            or item.project_id != batch.project_id
            for item in batch.relations
        ):
            raise ValueError("Semantic graph candidates must match extraction scope")
        if any(
            item.source_candidate_id not in entity_ids or item.target_candidate_id not in entity_ids
            for item in batch.relations
        ):
            raise ValueError("Semantic relation references an unknown entity candidate")
        await self.ensure_schema()
        parameters = {
            "tenant_id": batch.tenant_id,
            "project_id": batch.project_id,
            "document_id": str(batch.document_id),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        await self._driver.execute_query(
            Query(
                _ARCHIVE_STALE_PENDING_SEMANTIC_QUERY,
                timeout=self._timeout_seconds,
            ),
            parameters_={
                **parameters,
                "active_entity_ids": [str(candidate.candidate_id) for candidate in batch.entities],
                "active_relation_ids": [
                    str(candidate.candidate_id) for candidate in batch.relations
                ],
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )
        await self._driver.execute_query(
            Query(_INDEX_ENTITY_CANDIDATES_QUERY, timeout=self._timeout_seconds),
            parameters_={
                **parameters,
                "entities": [self._entity_candidate_payload(item) for item in batch.entities],
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )
        await self._driver.execute_query(
            Query(_INDEX_RELATION_CANDIDATES_QUERY, timeout=self._timeout_seconds),
            parameters_={
                **parameters,
                "relations": [self._relation_candidate_payload(item) for item in batch.relations],
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def set_entity_status(self, candidate: GraphEntityCandidate) -> None:
        await self.ensure_schema()
        await self._driver.execute_query(
            Query(_SET_ENTITY_STATUS_QUERY, timeout=self._timeout_seconds),
            parameters_=self._candidate_status_parameters(candidate),
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def set_relation_status(self, candidate: GraphRelationCandidate) -> None:
        await self.ensure_schema()
        await self._driver.execute_query(
            Query(_SET_RELATION_STATUS_QUERY, timeout=self._timeout_seconds),
            parameters_=self._candidate_status_parameters(candidate),
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def index_resolutions(
        self,
        candidates: Sequence[EntityResolutionCandidate],
    ) -> None:
        if not candidates:
            return
        tenant_id = candidates[0].tenant_id
        project_id = candidates[0].project_id
        if any(
            item.tenant_id != tenant_id
            or item.project_id != project_id
            or item.left_document_id == item.right_document_id
            or item.left_entity_id == item.right_entity_id
            for item in candidates
        ):
            raise ValueError("Entity resolution candidates must share a valid scope")
        await self.ensure_schema()
        await self._driver.execute_query(
            Query(_INDEX_ENTITY_RESOLUTIONS_QUERY, timeout=self._timeout_seconds),
            parameters_={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "resolutions": [self._resolution_candidate_payload(item) for item in candidates],
                "updated_at": datetime.now(UTC).isoformat(),
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def set_resolution_status(
        self,
        candidate: EntityResolutionCandidate,
    ) -> None:
        await self.ensure_schema()
        await self._driver.execute_query(
            Query(_SET_RESOLUTION_STATUS_QUERY, timeout=self._timeout_seconds),
            parameters_=self._candidate_status_parameters(candidate),
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )

    async def prune_pending_resolutions(
        self,
        kept_candidate_ids: Sequence[UUID],
        *,
        tenant_id: str,
        project_id: str,
    ) -> int:
        if not tenant_id.strip() or not project_id.strip():
            raise ValueError("Resolution pruning requires a tenant and project scope")
        await self.ensure_schema()
        result = await self._driver.execute_query(
            Query(
                _PRUNE_PENDING_ENTITY_RESOLUTIONS_QUERY,
                timeout=max(self._timeout_seconds, 120.0),
            ),
            parameters_={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "kept_candidate_ids": sorted({str(item) for item in kept_candidate_ids}),
            },
            routing_=RoutingControl.WRITE,
            database_=self._database,
        )
        if not result.records:
            return 0
        return int(result.records[0]["deleted_count"])

    async def reconcile_stale_pending_candidates(
        self,
        *,
        tenant_id: str,
        project_id: str,
        dry_run: bool = False,
    ) -> tuple[int, int, int]:
        """Archive pending semantic candidates backed by inactive chunk revisions."""

        if not tenant_id.strip() or not project_id.strip():
            raise ValueError("Candidate reconciliation requires a tenant and project scope")
        await self.ensure_schema()
        parameters = {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "dry_run": dry_run,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        stale_counts: list[int] = []
        for query in (
            _RECONCILE_STALE_PENDING_ENTITIES_QUERY,
            _RECONCILE_STALE_PENDING_RELATIONS_QUERY,
            _RECONCILE_STALE_PENDING_RESOLUTIONS_QUERY,
        ):
            result = await self._driver.execute_query(
                Query(query, timeout=max(self._timeout_seconds, 120.0)),
                parameters_=parameters,
                routing_=RoutingControl.WRITE,
                database_=self._database,
            )
            stale_counts.append(int(result.records[0]["stale_count"]) if result.records else 0)
        return stale_counts[0], stale_counts[1], stale_counts[2]

    async def search_graph(
        self,
        request: GraphSearchRequest,
        context: RunContext,
    ) -> GraphSearchResult:
        cypher: str | None
        if request.template == "paths":
            cypher = _PATH_TEMPLATES[request.max_hops]
        else:
            cypher = _TEMPLATES.get(request.template)
        if cypher is None:
            raise ValueError(f"Graph template is not allowlisted: {request.template}")
        parameters = {
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
            "entities": [entity.casefold() for entity in request.entities],
            "limit": request.limit,
            "knowledge_layers": [item.value for item in (context.enabled_knowledge_layers or ())],
            "user_id": context.user_id,
        }
        result = await self._driver.execute_query(
            Query(cypher, timeout=self._timeout_seconds),
            parameters_=parameters,
            routing_=RoutingControl.READ,
            database_=self._database,
        )
        records = cast(Sequence[Any], getattr(result, "records", result))
        paths: list[GraphPath] = []
        rejected = 0
        for record in records:
            raw_data = record.data() if hasattr(record, "data") else record
            if not isinstance(raw_data, Mapping):
                rejected += 1
                continue
            data = cast(Mapping[str, Any], raw_data)
            path = self._parse_path(data, context)
            if path is None:
                rejected += 1
                continue
            paths.append(path)
            if len(paths) >= request.limit:
                break
        evidence_by_id = {item.evidence_id: item for path in paths for item in path.evidence}
        summary = getattr(result, "summary", None)
        return GraphSearchResult(
            paths=paths,
            evidence=list(evidence_by_id.values()),
            trace={
                "backend": "neo4j",
                "database": self._database,
                "template": request.template,
                "max_hops": request.max_hops,
                "returned_paths": len(paths),
                "rejected_scope_or_evidence": rejected,
                "result_available_after_ms": getattr(summary, "result_available_after", None),
            },
        )

    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult:
        result = await self._driver.execute_query(
            Query(_ENTITY_RESOLUTION_QUERY, timeout=self._timeout_seconds),
            parameters_={
                "tenant_id": context.tenant_id,
                "project_id": context.project_id,
                "mentions": [mention.casefold() for mention in request.mentions],
                "entity_types": [item.casefold() for item in request.entity_types],
                "min_score": request.min_score,
                "limit": request.limit,
                "knowledge_layers": [
                    item.value for item in (context.enabled_knowledge_layers or ())
                ],
                "user_id": context.user_id,
            },
            routing_=RoutingControl.READ,
            database_=self._database,
        )
        records = cast(Sequence[Any], getattr(result, "records", result))
        matches: list[GraphEntityMatch] = []
        rejected = 0
        for record in records:
            raw_data = record.data() if hasattr(record, "data") else record
            if not isinstance(raw_data, Mapping):
                rejected += 1
                continue
            match = self._parse_entity_match(
                cast(Mapping[str, Any], raw_data),
                request,
                context,
            )
            if match is None:
                rejected += 1
                continue
            matches.append(match)
            if len(matches) >= request.limit:
                break
        evidence_by_id = {item.evidence_id: item for match in matches for item in match.evidence}
        summary = getattr(result, "summary", None)
        return GraphEntityResolveResult(
            mentions=request.mentions,
            matches=matches,
            evidence=list(evidence_by_id.values()),
            trace={
                "backend": "neo4j",
                "database": self._database,
                "strategy": "canonical_alias_deterministic",
                "returned_matches": len(matches),
                "rejected_scope_or_evidence": rejected,
                "result_available_after_ms": getattr(summary, "result_available_after", None),
            },
        )

    async def verify_connectivity(self) -> None:
        await self._driver.execute_query(
            "RETURN 1 AS ready",
            routing_=RoutingControl.READ,
            database_=self._database,
        )

    async def close(self) -> None:
        await self._driver.close()

    @staticmethod
    def _chunk_payload(
        document: KnowledgeDocument,
        chunk: KnowledgeChunk,
    ) -> dict[str, Any]:
        compact_name = " ".join(chunk.text.split())[:160]
        region_id = chunk.metadata.get("visual_region_id")
        source_fragment = f"#chunk={chunk.chunk_index}"
        if isinstance(region_id, str) and region_id:
            source_fragment += f"&region={region_id}"
        return {
            "node_id": str(chunk.chunk_id),
            "chunk_id": str(chunk.chunk_id),
            "name": compact_name or f"{document.title} chunk {chunk.chunk_index + 1}",
            "text": chunk.text,
            "source_type": document.source.source_type,
            "source_id": (f"{document.source.source_id or document.document_id}{source_fragment}"),
            "source_revision": document.source.source_revision,
            "canonical_uri": document.source.canonical_uri,
            "license_uri": document.source.license_uri,
            "privacy": document.source.privacy,
            "trust": document.source.trust.value,
            "content_hash": chunk.content_hash,
            "page_number": chunk.page_number,
            "chunk_index": chunk.chunk_index,
            "modality": chunk.metadata.get("modality"),
            "visual_kind": chunk.metadata.get("visual_kind"),
            "visual_region_id": region_id,
            "visual_category": chunk.metadata.get("visual_category"),
            "visual_bbox": chunk.metadata.get("visual_bbox"),
            "vision_confidence": chunk.metadata.get("vision_confidence"),
            "document_ir_schema": chunk.metadata.get("document_ir_schema"),
            "parser_revision": chunk.metadata.get("parser_revision"),
            "chunker_revision": chunk.metadata.get("chunker_revision"),
            "chunk_strategy": chunk.metadata.get("chunk_strategy"),
            "chunk_level": chunk.metadata.get("chunk_level"),
            "parent_section_id": chunk.metadata.get("parent_section_id"),
            "section_id": chunk.metadata.get("section_id"),
            "heading_path": chunk.metadata.get("heading_path", []),
            "block_ids": chunk.metadata.get("block_ids", []),
            "block_kinds": chunk.metadata.get("block_kinds", []),
            "page_start": chunk.metadata.get("page_start", chunk.page_number),
            "page_end": chunk.metadata.get("page_end", chunk.page_number),
            "extraction_methods": chunk.metadata.get("extraction_methods", []),
            "ocr_confidence_min": chunk.metadata.get("ocr_confidence_min"),
            "token_count": chunk.metadata.get("token_count"),
            "relationship_id": f"{document.document_id}:has_chunk:{chunk.chunk_id}",
        }

    @staticmethod
    def _entity_candidate_payload(candidate: GraphEntityCandidate) -> dict[str, Any]:
        return {
            "node_id": str(candidate.candidate_id),
            "candidate_id": str(candidate.candidate_id),
            "canonical_name": candidate.canonical_name,
            "entity_type": candidate.entity_type,
            "aliases": candidate.aliases,
            "source_chunk_ids": [str(item) for item in candidate.source_chunk_ids],
            "confidence": candidate.confidence,
            "extractor_version": candidate.extractor_revision,
            "domain_pack": candidate.domain_pack,
            "activation_policy": candidate.activation_policy,
            "candidate_status": candidate.status.value,
            "graph_status": _graph_status(candidate.status.value),
            "reviewed_by": candidate.reviewed_by,
            "reviewed_at": (candidate.reviewed_at.isoformat() if candidate.reviewed_at else None),
        }

    @staticmethod
    def _relation_candidate_payload(candidate: GraphRelationCandidate) -> dict[str, Any]:
        return {
            "relationship_id": str(candidate.candidate_id),
            "candidate_id": str(candidate.candidate_id),
            "source_node_id": str(candidate.source_candidate_id),
            "target_node_id": str(candidate.target_candidate_id),
            "relation_type": candidate.relation_type,
            "source_chunk_ids": [str(item) for item in candidate.source_chunk_ids],
            "confidence": candidate.confidence,
            "extractor_version": candidate.extractor_revision,
            "domain_pack": candidate.domain_pack,
            "activation_policy": candidate.activation_policy,
            "candidate_status": candidate.status.value,
            "graph_status": _graph_status(candidate.status.value),
            "reviewed_by": candidate.reviewed_by,
            "reviewed_at": (candidate.reviewed_at.isoformat() if candidate.reviewed_at else None),
        }

    @staticmethod
    def _resolution_candidate_payload(
        candidate: EntityResolutionCandidate,
    ) -> dict[str, Any]:
        return {
            "relationship_id": str(candidate.candidate_id),
            "candidate_id": str(candidate.candidate_id),
            "left_node_id": str(candidate.left_entity_id),
            "right_node_id": str(candidate.right_entity_id),
            "canonical_name": candidate.canonical_name,
            "match_strategy": candidate.match_strategy,
            "source_chunk_ids": [str(item) for item in candidate.source_chunk_ids],
            "confidence": candidate.confidence,
            "resolver_version": candidate.resolver_revision,
            "candidate_status": candidate.status.value,
            "graph_status": _graph_status(candidate.status.value),
            "reviewed_by": candidate.reviewed_by,
            "reviewed_at": (candidate.reviewed_at.isoformat() if candidate.reviewed_at else None),
        }

    @staticmethod
    def _candidate_status_parameters(
        candidate: (GraphEntityCandidate | GraphRelationCandidate | EntityResolutionCandidate),
    ) -> dict[str, Any]:
        return {
            "tenant_id": candidate.tenant_id,
            "project_id": candidate.project_id,
            "candidate_id": str(candidate.candidate_id),
            "candidate_status": candidate.status.value,
            "graph_status": _graph_status(candidate.status.value),
            "reviewed_by": candidate.reviewed_by,
            "reviewed_at": (candidate.reviewed_at.isoformat() if candidate.reviewed_at else None),
            "updated_at": candidate.updated_at.isoformat(),
        }

    @staticmethod
    def _parse_path(data: Mapping[str, Any], context: RunContext) -> GraphPath | None:
        raw_nodes = data.get("nodes")
        raw_relationships = data.get("relationships")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, str | bytes):
            return None
        if not isinstance(raw_relationships, Sequence) or isinstance(
            raw_relationships, str | bytes
        ):
            return None
        try:
            nodes = [
                GraphNode.model_validate(_json_value(item))
                for item in raw_nodes
                if isinstance(item, Mapping)
            ]
            relationships = [
                GraphRelationship.model_validate(_json_value(item))
                for item in raw_relationships
                if isinstance(item, Mapping)
            ]
        except (TypeError, ValueError):
            return None
        if len(nodes) != len(raw_nodes) or len(relationships) != len(raw_relationships):
            return None
        if not nodes or not relationships:
            return None
        if any(
            node.tenant_id != context.tenant_id or node.project_id != context.project_id
            for node in nodes
        ):
            return None
        if any(
            relationship.tenant_id != context.tenant_id
            or relationship.project_id != context.project_id
            or not relationship.evidence
            for relationship in relationships
        ):
            return None
        node_ids = {node.node_id for node in nodes}
        if any(
            relationship.source_node_id not in node_ids
            or relationship.target_node_id not in node_ids
            for relationship in relationships
        ):
            return None
        evidence = [item for relationship in relationships for item in relationship.evidence]
        return GraphPath(
            nodes=nodes,
            relationships=relationships,
            evidence=evidence,
        )

    @staticmethod
    def _parse_entity_match(
        data: Mapping[str, Any],
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityMatch | None:
        raw_node = data.get("node")
        raw_evidence = data.get("evidence")
        if not isinstance(raw_node, Mapping):
            return None
        if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, str | bytes):
            return None
        try:
            node = GraphNode.model_validate(_json_value(raw_node))
            evidence = [
                EvidenceRef.model_validate(_json_value(item))
                for item in raw_evidence
                if isinstance(item, Mapping)
            ]
            match = GraphEntityMatch(
                node=node,
                matched_text=str(data.get("matched_text", "")),
                matched_field=str(data.get("matched_field", "")),
                score=float(data.get("score", 0.0)),
                evidence=evidence,
            )
        except (TypeError, ValueError):
            return None
        if (
            node.tenant_id != context.tenant_id
            or node.project_id != context.project_id
            or not evidence
            or match.score < request.min_score
            or match.matched_text.casefold()
            not in {mention.casefold() for mention in request.mentions}
        ):
            return None
        return match


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_json_value(item) for item in value]
    if isinstance(value, Provenance | EvidenceRef):
        return value.model_dump(mode="json")
    iso_format = getattr(value, "iso_format", None)
    if callable(iso_format):
        return iso_format()
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        return _json_value(to_native())
    return str(value)


def _graph_status(candidate_status: str) -> str:
    return {
        "approved": "active",
        "rejected": "rejected",
        "archived": "archived",
    }.get(candidate_status, "candidate")

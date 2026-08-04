from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from typing import Any, cast

from neo4j import AsyncGraphDatabase, RoutingControl
from qdrant_client import AsyncQdrantClient

from app.domain.models import (
    GraphSearchRequest,
    KnowledgeChunk,
    KnowledgeDocument,
    RunContext,
)
from app.graph.neo4j import Neo4jEvidenceGraph
from app.retrieval.embeddings import DeterministicDenseEmbedder, HashedSparseEmbedder
from app.retrieval.qdrant_hybrid import QdrantHybridStore

TENANT_ID = "smoke"
PROJECT_ID = "adapter-smoke"
COLLECTION_NAME = "hermesgraph_smoke_chunks"


async def _smoke_qdrant(url: str) -> dict[str, Any]:
    client = AsyncQdrantClient(url=url)
    store = QdrantHybridStore(
        client,
        DeterministicDenseEmbedder(64),
        HashedSparseEmbedder(),
        collection_name=COLLECTION_NAME,
    )
    content = "AURORA-ADAPTER-910 requires a signed copper key before launch."
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    document = KnowledgeDocument(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        filename="infrastructure-smoke.md",
        title="Infrastructure smoke fixture",
        media_type="text/markdown",
        byte_size=len(content.encode("utf-8")),
        content_hash=content_hash,
        storage_key="smoke/infrastructure-smoke.md",
        chunk_count=1,
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        chunk_index=0,
        text=content,
        content_hash=content_hash,
        char_end=len(content),
    )
    try:
        await store.index_document(document, [chunk])
        evidence = await store.retrieve(
            "What does AURORA-ADAPTER-910 require?",
            RunContext(tenant_id=TENANT_ID, project_id=PROJECT_ID),
        )
        if not evidence or "signed copper key" not in evidence[0].text:
            raise RuntimeError("Qdrant did not return the indexed smoke fixture")
        await store.archive_document(
            document.document_id,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
        )
        archived = await store.retrieve(
            "AURORA-ADAPTER-910",
            RunContext(tenant_id=TENANT_ID, project_id=PROJECT_ID),
        )
        if archived:
            raise RuntimeError("Qdrant returned an archived smoke fixture")
        return {
            "backend": "qdrant",
            "status": "ok",
            "evidence_source": evidence[0].provenance.source_type,
            "retrieval_backend": evidence[0].metadata["retrieval_backend"],
        }
    finally:
        if await client.collection_exists(COLLECTION_NAME):
            await cast(Any, client).delete_collection(COLLECTION_NAME)
        await client.close()


async def _smoke_neo4j(
    uri: str,
    user: str,
    password: str,
    database: str,
) -> dict[str, Any]:
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    graph = Neo4jEvidenceGraph(driver, database=database)
    cleanup = """
MATCH (node {tenant_id: $tenant_id, project_id: $project_id})
DETACH DELETE node
"""
    parameters = {
        "tenant_id": TENANT_ID,
        "project_id": PROJECT_ID,
    }
    text = "AURORA-GRAPH-910 requires an evidence-backed relationship."
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    document = KnowledgeDocument(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        filename="graph-smoke.md",
        title="Infrastructure Graph Smoke",
        media_type="text/markdown",
        byte_size=len(text.encode("utf-8")),
        content_hash=content_hash,
        storage_key="smoke/graph-smoke.md",
        chunk_count=1,
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        chunk_index=0,
        text=text,
        content_hash=content_hash,
        char_end=len(text),
    )
    try:
        await graph.verify_connectivity()
        await driver.execute_query(
            cleanup,
            parameters_=parameters,
            routing_=RoutingControl.WRITE,
            database_=database,
        )
        await graph.index_document(document, [chunk])
        result = await graph.search_graph(
            GraphSearchRequest(entities=["infrastructure graph smoke"], template="neighbors"),
            RunContext(tenant_id=TENANT_ID, project_id=PROJECT_ID),
        )
        if len(result.paths) != 1:
            raise RuntimeError("Neo4j did not return exactly one smoke path")
        path = result.paths[0]
        if not path.relationships[0].evidence:
            raise RuntimeError("Neo4j returned a relationship without evidence")
        if path.relationships[0].evidence[0].provenance.source_id != (
            f"{document.document_id}:0"
        ):
            raise RuntimeError("Neo4j returned unexpected graph provenance")
        await graph.archive_document(
            document.document_id,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
        )
        archived = await graph.search_graph(
            GraphSearchRequest(entities=["infrastructure graph smoke"]),
            RunContext(tenant_id=TENANT_ID, project_id=PROJECT_ID),
        )
        if archived.paths:
            raise RuntimeError("Neo4j returned an archived document path")
        return {
            "backend": "neo4j",
            "status": "ok",
            "paths": len(result.paths),
            "evidence": len(result.evidence),
            "template": result.trace["template"],
            "archived_hidden": True,
        }
    finally:
        try:
            await driver.execute_query(
                cleanup,
                parameters_=parameters,
                routing_=RoutingControl.WRITE,
                database_=database,
            )
        finally:
            await graph.close()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Verify live Qdrant and Neo4j adapters")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument("--neo4j-password", default="hermesgraph-dev")
    parser.add_argument("--neo4j-database", default="neo4j")
    args = parser.parse_args()

    qdrant, neo4j = await asyncio.gather(
        _smoke_qdrant(args.qdrant_url),
        _smoke_neo4j(
            args.neo4j_uri,
            args.neo4j_user,
            args.neo4j_password,
            args.neo4j_database,
        ),
    )
    print(json.dumps({"qdrant": qdrant, "neo4j": neo4j}, indent=2))


if __name__ == "__main__":
    asyncio.run(_main())

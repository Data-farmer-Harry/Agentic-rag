from typing import Any

from app.domain.models import DomainPackManifest


class GeneralDomainPack:
    name = "general"

    def manifest(self) -> DomainPackManifest:
        return DomainPackManifest(
            pack_id=self.name,
            version="0.2.0",
            core_compatibility=">=0.1,<0.2",
            description="Conservative domain-neutral knowledge work defaults.",
            capability_names=[
                "search_knowledge",
                "search_graph",
                "resolve_graph_entities",
                "retrieve_evidence_subgraph",
                "compare_graph_entities",
            ],
            required_scopes=["knowledge:read", "graph:read"],
            schema_hash="general-v2",
        )

    def system_context(self) -> str:
        return (
            "No specialist domain pack is active. Use conservative general-purpose reasoning, "
            "preserve source boundaries, and state when domain expertise is required."
        )

    def graph_templates(self) -> dict[str, str]:
        return {
            "neighbors": "Find evidence-backed neighbors for a canonical entity.",
            "paths": "Find bounded evidence-backed paths between canonical entities.",
            "conflicts": "Find claims with incompatible qualifiers or conclusions.",
        }

    def output_schemas(self) -> dict[str, type[Any]]:
        return {}

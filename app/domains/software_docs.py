from app.domain.models import DomainPackManifest
from app.domains.general import GeneralDomainPack


class SoftwareDocsReferenceDomainPack(GeneralDomainPack):
    name = "software_docs_reference"

    def manifest(self) -> DomainPackManifest:
        return DomainPackManifest(
            pack_id=self.name,
            version="0.2.0",
            core_compatibility=">=0.1,<0.2",
            description="Reference pack for APIs, libraries, repositories, and operations docs.",
            capability_names=[
                "search_knowledge",
                "search_graph",
                "resolve_graph_entities",
                "retrieve_evidence_subgraph",
                "compare_graph_entities",
            ],
            required_scopes=["knowledge:read", "graph:read"],
            schema_hash="software-docs-v2",
        )

    def system_context(self) -> str:
        return (
            "For software documentation, preserve product and version identifiers. Distinguish "
            "documented behavior, observed implementation, and migration advice. Prefer official "
            "documentation and source code over secondary summaries."
        )

    def graph_templates(self) -> dict[str, str]:
        templates = super().graph_templates()
        templates.update(
            {
                "component_dependency": "Connect components, packages, and runtime dependencies.",
                "api_compatibility": "Connect API symbols to versions and compatibility notes.",
            }
        )
        return templates

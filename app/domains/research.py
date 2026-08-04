from app.domain.models import DomainPackManifest
from app.domains.general import GeneralDomainPack


class ResearchReferenceDomainPack(GeneralDomainPack):
    """Reference pack used by eval fixtures; it is not a core runtime dependency."""

    name = "research_reference"

    def manifest(self) -> DomainPackManifest:
        return DomainPackManifest(
            pack_id=self.name,
            version="0.2.0",
            core_compatibility=">=0.1,<0.2",
            description="Reference pack for papers, methods, datasets, metrics, and experiments.",
            capability_names=[
                "search_knowledge",
                "search_graph",
                "resolve_graph_entities",
                "retrieve_evidence_subgraph",
                "compare_graph_entities",
            ],
            required_scopes=["knowledge:read", "graph:read"],
            schema_hash="research-reference-v2",
        )

    def system_context(self) -> str:
        return (
            "For research questions, separate definitions, mechanisms, empirical results, and "
            "limitations. Prefer primary sources, preserve publication dates, and label synthesis "
            "across sources as inference."
        )

    def graph_templates(self) -> dict[str, str]:
        templates = super().graph_templates()
        templates.update(
            {
                "method_dataset_metric": "Connect method, dataset, metric, and reported result.",
                "method_lineage": "Trace improves, extends, and baseline-of relationships.",
            }
        )
        return templates

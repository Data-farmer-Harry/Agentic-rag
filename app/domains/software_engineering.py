from __future__ import annotations

from typing import Any

from app.domain.models import DomainPackManifest
from app.domains.general import GeneralDomainPack


class SoftwareEngineeringDomainPack(GeneralDomainPack):
    """Ontology and query vocabulary for an R&D team's engineering workspace."""

    name = "software_engineering"

    _ENTITY_TYPES = (
        "Service",
        "Repository",
        "Module",
        "API",
        "Database",
        "Queue",
        "Model",
        "Team",
        "Person",
        "Decision",
        "Incident",
        "Runbook",
        "Requirement",
        "Environment",
        "Metric",
        "FeatureFlag",
        "Technology",
        "Document",
    )
    _RELATION_TYPES = (
        "DEPENDS_ON",
        "ROUTES_TO",
        "CALLS",
        "EXPOSES",
        "READS_FROM",
        "STORES_IN",
        "PUBLISHES_TO",
        "CONSUMES_FROM",
        "OWNED_BY",
        "ON_CALL_BY",
        "DOCUMENTED_BY",
        "DECIDED_BY",
        "SUPERSEDES",
        "CAUSED_BY",
        "MITIGATED_BY",
        "AFFECTED",
        "MONITORED_BY",
        "CONTROLLED_BY",
        "IMPLEMENTED_IN",
        "RELATED_TO",
    )

    def manifest(self) -> DomainPackManifest:
        return DomainPackManifest(
            pack_id=self.name,
            version="1.0.0",
            core_compatibility=">=0.1,<0.2",
            description=(
                "Engineering intelligence pack for source-aware architecture, "
                "operations, decisions, and delivery knowledge."
            ),
            capability_names=[
                "search_knowledge",
                "search_graph",
                "resolve_graph_entities",
                "retrieve_evidence_subgraph",
                "compare_graph_entities",
            ],
            required_scopes=["knowledge:read", "graph:read"],
            schema_hash="software-engineering-v1",
        )

    def system_context(self) -> str:
        return (
            "Use the software engineering ontology when it clarifies the answer. "
            "Separate documented intent, observed behavior, and incident evidence. "
            "Treat source status and temporal validity as first-class qualifiers; "
            "prefer active decisions and runbooks, and name superseded material."
        )

    def graph_templates(self) -> dict[str, str]:
        templates = super().graph_templates()
        templates.update(
            {
                "service_dependencies": (
                    "Trace bounded Service, API, Database, and Queue dependencies."
                ),
                "service_ownership": "Find a Service's owning Team, on-call Person, and runbook.",
                "incident_impact": (
                    "Connect an Incident to affected services, metrics, mitigations, "
                    "and decisions."
                ),
                "decision_lineage": (
                    "Trace a Decision, the documents supporting it, and superseding "
                    "decisions."
                ),
                "api_surface": (
                    "Connect an API to its implementing Service and downstream consumers."
                ),
            }
        )
        return templates

    def output_schemas(self) -> dict[str, type[Any]]:
        return {}

    def ontology(self) -> dict[str, tuple[str, ...]]:
        return {"entities": self._ENTITY_TYPES, "relations": self._RELATION_TYPES}

    def display_metadata(self) -> dict[str, dict[str, str]]:
        return {
            "Service": {"group": "architecture", "label": "Service"},
            "Incident": {"group": "operations", "label": "Incident"},
            "Decision": {"group": "governance", "label": "Decision"},
            "Runbook": {"group": "operations", "label": "Runbook"},
            "Team": {"group": "ownership", "label": "Team"},
        }

    def graph_activation_policy(self) -> dict[str, object]:
        """The fixture can create candidates, but only an owner review activates facts."""

        return {
            "mode": "review_required",
            "eligible_source_status": "active",
            "eligible_relation_types": self._RELATION_TYPES,
            "audit_event": "GraphCandidateReviewEvent",
        }

from pathlib import Path

from app.domains.registry import DomainPackRegistry


def test_all_domain_packs_satisfy_manifest_contract() -> None:
    registry = DomainPackRegistry()
    registry.validate()

    assert "research_reference" in registry.names()
    assert "software_docs_reference" in registry.names()
    assert (
        registry.get("research_reference").manifest().schema_hash
        != registry.get("software_docs_reference").manifest().schema_hash
    )


def test_core_does_not_embed_reference_domain_entity_types() -> None:
    root = Path("app")
    core_paths = [root / "agent", root / "application", root / "domain"]
    forbidden = {"method_dataset_metric", "method_lineage"}

    core_text = "\n".join(
        path.read_text(encoding="utf-8") for base in core_paths for path in base.rglob("*.py")
    ).casefold()

    assert not any(term in core_text for term in forbidden)

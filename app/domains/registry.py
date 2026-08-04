from app.domain.contracts import DomainPack
from app.domains.general import GeneralDomainPack
from app.domains.research import ResearchReferenceDomainPack
from app.domains.software_docs import SoftwareDocsReferenceDomainPack
from app.domains.software_engineering import SoftwareEngineeringDomainPack


class DomainPackRegistry:
    def __init__(self) -> None:
        self._packs: dict[str, DomainPack] = {}
        self.register(GeneralDomainPack())
        self.register(ResearchReferenceDomainPack())
        self.register(SoftwareDocsReferenceDomainPack())
        self.register(SoftwareEngineeringDomainPack())

    def register(self, pack: DomainPack) -> None:
        if pack.name in self._packs:
            raise ValueError(f"Domain pack already registered: {pack.name}")
        self._packs[pack.name] = pack

    def get(self, name: str) -> DomainPack:
        try:
            return self._packs[name]
        except KeyError as exc:
            raise KeyError(f"Unknown domain pack: {name}") from exc

    def names(self) -> list[str]:
        return sorted(self._packs)

    def validate(self) -> None:
        for name, pack in self._packs.items():
            manifest = pack.manifest()
            if manifest.pack_id != name:
                raise ValueError(f"Domain pack manifest mismatch: {name} != {manifest.pack_id}")

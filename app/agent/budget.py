from dataclasses import dataclass, field


class RunBudgetExceeded(RuntimeError):
    pass


@dataclass
class RunBudget:
    max_tool_calls: int
    tool_calls: int = 0
    fingerprints: set[str] = field(default_factory=set)

    def consume(self, fingerprint: str) -> None:
        if fingerprint in self.fingerprints:
            raise RunBudgetExceeded("Repeated tool call blocked")
        if self.tool_calls >= self.max_tool_calls:
            raise RunBudgetExceeded("Tool call budget exhausted")
        self.fingerprints.add(fingerprint)
        self.tool_calls += 1

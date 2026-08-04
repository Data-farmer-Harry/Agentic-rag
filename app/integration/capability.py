from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from app.domain.models import CapabilitySpec, RunContext


class CapabilityError(RuntimeError):
    """Base error for capability registration and invocation."""


class CapabilityNotFoundError(CapabilityError):
    pass


class CapabilityAlreadyRegisteredError(CapabilityError):
    pass


class CapabilityScopeError(CapabilityError):
    pass


class CapabilityOutputTooLargeError(CapabilityError):
    pass


CapabilityHandler = Callable[
    [Mapping[str, Any], RunContext | None, Mapping[str, Any]],
    Any | Awaitable[Any],
]


def _serialized_size(value: Any) -> int:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        encoded = json.dumps(value, default=str, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapabilityError("Capability output is not serializable") from exc
    return len(encoded)


def _json_value(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _validate_schema(value: Any, schema: Mapping[str, Any], *, label: str) -> None:
    if not schema:
        return
    errors = sorted(
        Draft202012Validator(dict(schema)).iter_errors(_json_value(value)),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.absolute_path) or "$"
        raise CapabilityError(
            f"Capability {label} schema validation failed at {path}: {first.message}"
        )


@dataclass(frozen=True, slots=True)
class Capability:
    """Framework-neutral executable form of a CapabilitySpec."""

    spec: CapabilitySpec
    handler: CapabilityHandler

    async def ainvoke(
        self,
        payload: Mapping[str, Any],
        *,
        context: RunContext | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        _validate_schema(payload, self.spec.input_schema, label="input")

        async def execute() -> Any:
            result = self.handler(payload, context, metadata or {})
            if inspect.isawaitable(result):
                return await result
            return result

        try:
            result = await asyncio.wait_for(execute(), timeout=self.spec.timeout_seconds)
        except TimeoutError as exc:
            raise CapabilityError(
                f"Capability {self.spec.name}@{self.spec.version} timed out after "
                f"{self.spec.timeout_seconds}s"
            ) from exc

        size = _serialized_size(result)
        if size > self.spec.max_output_bytes:
            raise CapabilityOutputTooLargeError(
                f"Capability {self.spec.name}@{self.spec.version} returned {size} bytes; "
                f"limit is {self.spec.max_output_bytes}"
            )
        _validate_schema(result, self.spec.output_schema, label="output")
        return result


class CapabilityRegistry:
    """Version-aware registry for framework-neutral capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, dict[str, Capability]] = {}

    def register(self, capability: Capability, *, replace: bool = False) -> Capability:
        versions = self._capabilities.setdefault(capability.spec.name, {})
        version = capability.spec.version
        if version in versions and not replace:
            raise CapabilityAlreadyRegisteredError(
                f"Capability {capability.spec.name}@{version} is already registered"
            )
        versions[version] = capability
        return capability

    def unregister(self, name: str, version: str) -> bool:
        versions = self._capabilities.get(name)
        if not versions or version not in versions:
            return False
        del versions[version]
        if not versions:
            del self._capabilities[name]
        return True

    def resolve(self, name: str, version: str | None = None) -> Capability:
        versions = self._capabilities.get(name)
        if not versions:
            raise CapabilityNotFoundError(f"Capability {name!r} is not registered")
        selected_version = version or max(versions, key=self._version_key)
        try:
            return versions[selected_version]
        except KeyError as exc:
            raise CapabilityNotFoundError(
                f"Capability {name!r} has no registered version {selected_version!r}"
            ) from exc

    def list_specs(self) -> Sequence[CapabilitySpec]:
        capabilities = (
            capability
            for versions in self._capabilities.values()
            for capability in versions.values()
        )
        return tuple(
            item.spec
            for item in sorted(
                capabilities,
                key=lambda item: (item.spec.name, self._version_key(item.spec.version)),
            )
        )

    async def invoke(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        version: str | None = None,
        granted_scopes: Sequence[str] = (),
        context: RunContext | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        capability = self.resolve(name, version)
        missing = sorted(set(capability.spec.required_scopes) - set(granted_scopes))
        if missing:
            raise CapabilityScopeError(
                f"Capability {name!r} requires missing scopes: {', '.join(missing)}"
            )
        return await capability.ainvoke(payload, context=context, metadata=metadata)

    @staticmethod
    def _version_key(version: str) -> tuple[int, int, int]:
        major, minor, patch = version.split(".")
        return int(major), int(minor), int(patch)

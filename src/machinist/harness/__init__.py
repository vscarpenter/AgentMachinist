"""Versioned Harness registry with isolated Python entry-point discovery."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from importlib.metadata import entry_points as metadata_entry_points
from typing import Any, Iterable

from machinist.config import HarnessConfig, HarnessIdentifier, harness_identifier
from machinist.harness.base import Harness, HarnessDescriptor, HarnessError, Runner
from machinist.harness.claude_code import ClaudeCode
from machinist.harness.codex import Codex
from machinist.harness.opencode import OpenCode
from machinist.harness.pi import Pi

HARNESS_ENTRY_POINT_GROUP = "agentmachinist.harnesses.v1"
_BUILTINS: dict[str, type[Harness]] = {
    cls.name: cls for cls in (ClaudeCode, OpenCode, Pi, Codex)
}
_PHASES = frozenset({"spec", "execute", "review"})


@dataclass(frozen=True)
class HarnessDiscoveryFailure:
    entry_point: str
    value: str
    message: str


@dataclass(frozen=True)
class HarnessRegistry:
    adapters: dict[str, type[Harness]]
    failures: tuple[HarnessDiscoveryFailure, ...] = ()


def discover_harnesses(*, entry_points: Iterable[Any] | None = None) -> HarnessRegistry:
    """Load healthy v1 adapters while preserving isolated failure details."""
    adapters = dict(_BUILTINS)
    failures: list[HarnessDiscoveryFailure] = []
    for entry_point in _selected_entry_points(entry_points):
        failure = _load_entry_point(entry_point, adapters)
        if failure is not None:
            failures.append(failure)
    return HarnessRegistry(adapters=adapters, failures=tuple(failures))


def get_harness(
    config: HarnessConfig,
    runner: Runner = subprocess.run,
    *,
    entry_points: Iterable[Any] | None = None,
) -> Harness:
    """Construct the configured built-in or discovered adapter."""
    registry = discover_harnesses(entry_points=entry_points)
    name = harness_identifier(config.name)
    adapter = registry.adapters.get(name)
    if adapter is None:
        raise HarnessError(_unknown_adapter_message(name, registry))
    return adapter(config, runner=runner)


def get_harness_descriptor(
    name: HarnessIdentifier,
    *,
    entry_points: Iterable[Any] | None = None,
) -> HarnessDescriptor:
    """Return validated metadata without constructing or running a Harness."""
    registry = discover_harnesses(entry_points=entry_points)
    identifier = harness_identifier(name)
    adapter = registry.adapters.get(identifier)
    if adapter is None:
        raise HarnessError(_unknown_adapter_message(identifier, registry))
    return adapter.descriptor


def _selected_entry_points(supplied: Iterable[Any] | None) -> tuple[Any, ...]:
    if supplied is not None:
        return tuple(supplied)
    discovered = metadata_entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=HARNESS_ENTRY_POINT_GROUP))
    return tuple(discovered.get(HARNESS_ENTRY_POINT_GROUP, ()))


def _load_entry_point(
    entry_point: Any, adapters: dict[str, type[Harness]]
) -> HarnessDiscoveryFailure | None:
    name = str(getattr(entry_point, "name", "<unnamed>"))
    value = str(getattr(entry_point, "value", "<unknown>"))
    try:
        if name in _BUILTINS:
            raise ValueError(f"adapter name '{name}' is a reserved built-in")
        loaded = entry_point.load()
        _validate_adapter(name, loaded)
        if name in adapters:
            raise ValueError(f"duplicate adapter name '{name}'")
        adapters[name] = loaded
    except Exception as exc:
        return HarnessDiscoveryFailure(name, value, str(exc))
    return None


def _validate_adapter(entry_name: str, loaded: object) -> None:
    if not isinstance(loaded, type) or not issubclass(loaded, Harness):
        raise ValueError("entry point must load a Harness subclass")
    if loaded.name != entry_name:
        raise ValueError(
            f"entry-point name '{entry_name}' must match adapter name '{loaded.name}'"
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", loaded.name):
        raise ValueError("adapter name is not a valid Harness identifier")
    descriptor = getattr(loaded, "descriptor", None)
    if not isinstance(descriptor, HarnessDescriptor):
        raise ValueError("adapter must declare a HarnessDescriptor")
    _validate_descriptor(descriptor)


def _validate_descriptor(descriptor: HarnessDescriptor) -> None:
    if descriptor.contract_version != 1:
        raise ValueError("adapter descriptor contract_version must be 1")
    if not descriptor.display_name.strip():
        raise ValueError("adapter descriptor display_name cannot be empty")
    if not descriptor.documentation_url.startswith("https://"):
        raise ValueError("adapter documentation_url must use HTTPS")
    if not descriptor.phases or not descriptor.phases <= _PHASES:
        raise ValueError("adapter phases must use spec, execute, or review")
    profile = descriptor.ci_spec
    if profile is None:
        return
    if not profile.install_argv or any(
        not part or "\x00" in part or "\n" in part for part in profile.install_argv
    ):
        raise ValueError("CI install argv must contain safe non-empty strings")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", profile.secret_env):
        raise ValueError("CI secret_env must be an uppercase environment name")


def _unknown_adapter_message(name: str, registry: HarnessRegistry) -> str:
    available = ", ".join(sorted(registry.adapters))
    message = (
        f"unknown harness adapter '{name}'; available: {available}. "
        f"Install plugins through entry-point group {HARNESS_ENTRY_POINT_GROUP}."
    )
    if registry.failures:
        details = "; ".join(
            f"{item.entry_point} ({item.value}): {item.message}"
            for item in registry.failures
        )
        message += f" Plugin load failures: {details}"
    return message


__all__ = [
    "HARNESS_ENTRY_POINT_GROUP",
    "Harness",
    "HarnessDiscoveryFailure",
    "HarnessError",
    "HarnessRegistry",
    "discover_harnesses",
    "get_harness",
    "get_harness_descriptor",
]

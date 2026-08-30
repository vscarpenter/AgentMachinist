"""Versioned third-party Harness entry-point contract."""

import subprocess

import pytest

from machinist.config import HarnessConfig
from machinist.harness import (
    HARNESS_ENTRY_POINT_GROUP,
    discover_harnesses,
    get_harness,
)
from machinist.harness.base import (
    Harness,
    HarnessCIProfile,
    HarnessDescriptor,
    HarnessError,
)


class PluginHarness(Harness):
    name = "acme-reviewer"
    default_command = "acme"
    descriptor = HarnessDescriptor(
        contract_version=1,
        display_name="Acme Reviewer",
        documentation_url="https://example.com/acme",
        phases=frozenset({"spec", "execute", "review"}),
        structured_usage=True,
        ci_spec=HarnessCIProfile(
            install_argv=("uv", "tool", "install", "acme-harness==1.2.3"),
            secret_env="ACME_API_KEY",
        ),
    )

    def spec_argv(self, prompt):
        return [self.command, "spec", prompt]

    def implement_argv(self, prompt):
        return [self.command, "run", prompt]


class FakeEntryPoint:
    group = HARNESS_ENTRY_POINT_GROUP

    def __init__(self, name, value, loaded=None, error=None):
        self.name = name
        self.value = value
        self._loaded = loaded
        self._error = error

    def load(self):
        if self._error:
            raise self._error
        return self._loaded


def test_discovery_loads_valid_plugin_without_replacing_builtins():
    entry = FakeEntryPoint("acme-reviewer", "acme:PluginHarness", loaded=PluginHarness)

    registry = discover_harnesses(entry_points=[entry])

    assert "claude-code" in registry.adapters
    assert registry.adapters["acme-reviewer"] is PluginHarness
    assert registry.failures == ()


def test_get_harness_constructs_discovered_adapter_with_injected_runner():
    entry = FakeEntryPoint("acme-reviewer", "acme:PluginHarness", loaded=PluginHarness)

    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")

    config = HarnessConfig.model_validate({"name": "acme-reviewer"})

    harness = get_harness(config, runner=runner, entry_points=[entry])

    assert isinstance(harness, PluginHarness)
    assert harness.command == "acme"


@pytest.mark.parametrize(
    "entry, expected",
    [
        (
            FakeEntryPoint("claude-code", "acme:PluginHarness", loaded=PluginHarness),
            "reserved built-in",
        ),
        (
            FakeEntryPoint(
                "different-name", "acme:PluginHarness", loaded=PluginHarness
            ),
            "must match",
        ),
        (
            FakeEntryPoint("broken", "acme:broken", error=RuntimeError("boom")),
            "boom",
        ),
    ],
)
def test_discovery_isolates_invalid_or_broken_plugins(entry, expected):
    registry = discover_harnesses(entry_points=[entry])

    assert len(registry.failures) == 1
    assert expected in registry.failures[0].message
    assert "claude-code" in registry.adapters


def test_unknown_adapter_error_lists_group_and_load_failures():
    entry = FakeEntryPoint("broken", "acme:broken", error=RuntimeError("boom"))
    config = HarnessConfig.model_validate({"name": "missing-adapter"})

    with pytest.raises(HarnessError, match=HARNESS_ENTRY_POINT_GROUP) as raised:
        get_harness(config, entry_points=[entry])

    assert "broken" in str(raised.value)
    assert "claude-code" in str(raised.value)

"""Harness registry: config name → adapter."""

from __future__ import annotations

import subprocess

from machinist.config import HarnessConfig
from machinist.harness.base import Harness, HarnessError, Runner
from machinist.harness.claude_code import ClaudeCode
from machinist.harness.codex import Codex
from machinist.harness.opencode import OpenCode
from machinist.harness.pi import Pi

_REGISTRY: dict[str, type[Harness]] = {
    cls.name: cls for cls in (ClaudeCode, OpenCode, Pi, Codex)
}


def get_harness(config: HarnessConfig, runner: Runner = subprocess.run) -> Harness:
    return _REGISTRY[config.name.value](config, runner=runner)


__all__ = ["Harness", "HarnessError", "get_harness"]

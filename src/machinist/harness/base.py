"""Base class for coding-harness adapters.

An adapter's job is small on purpose: build the argv that runs its CLI
headlessly for the two phases. Subprocess mechanics, timeouts, and error
translation live here so adapters stay one screen long.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, ClassVar

from machinist.config import HarnessConfig

Runner = Callable[..., subprocess.CompletedProcess]


class HarnessError(Exception):
    """A harness invocation failed or timed out."""


class Harness(ABC):
    name: ClassVar[str]
    default_command: ClassVar[str]

    def __init__(self, config: HarnessConfig, runner: Runner = subprocess.run):
        self.config = config
        self._runner = runner

    @property
    def command(self) -> str:
        return self.config.command or self.default_command

    @abstractmethod
    def spec_argv(self, prompt: str) -> list[str]:
        """Argv that makes the harness read a prompt and print a spec to stdout."""

    @abstractmethod
    def implement_argv(self, prompt: str) -> list[str]:
        """Argv that makes the harness edit files headlessly per the prompt."""

    def generate_spec(self, prompt: str, cwd: Path) -> str:
        return self._run(self.spec_argv(prompt), cwd, self.config.spec_timeout_minutes)

    def implement(self, prompt: str, cwd: Path) -> str:
        return self._run(self.implement_argv(prompt), cwd, self.config.timeout_minutes)

    def _run(self, argv: list[str], cwd: Path, timeout_minutes: int) -> str:
        try:
            result = self._runner(
                argv, cwd=cwd, timeout=timeout_minutes * 60,
                capture_output=True, text=True,
            )
        except FileNotFoundError as exc:
            raise HarnessError(
                f"harness executable '{argv[0]}' not found; install it or set harness.command"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(f"{self.name} timed out after {timeout_minutes} minutes") from exc
        if result.returncode != 0:
            raise HarnessError(
                f"{self.name} exited with {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout

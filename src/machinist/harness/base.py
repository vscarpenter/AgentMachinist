"""Base class for coding-harness adapters.

An adapter's job is small on purpose: build the argv that runs its CLI
headlessly for the two phases. Subprocess mechanics, timeouts, and error
translation live here so adapters stay one screen long.
"""

from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, ClassVar

from machinist.config import HarnessConfig

Runner = Callable[..., subprocess.CompletedProcess]


class HarnessError(Exception):
    """A harness invocation failed or timed out."""


@dataclass(frozen=True)
class HarnessCapabilities:
    """Controls the adapter actually requests, not security guarantees."""

    spec_repository_writes: str
    implementation_git_control: str = "prompt-and-postcondition"


_CONTROLLER_CREDENTIALS = {
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
}


class Harness(ABC):
    name: ClassVar[str]
    default_command: ClassVar[str]
    capabilities: ClassVar[HarnessCapabilities] = HarnessCapabilities("advisory")

    # Harness runs are silent and can last many minutes; a periodic progress
    # callback keeps callers (and humans) sure the process is alive.
    heartbeat_seconds: float = 30.0
    on_progress: Callable[[str], None] | None = None

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
            result = self._run_with_heartbeat(argv, cwd, timeout_minutes)
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

    def _run_with_heartbeat(self, argv: list[str], cwd: Path, timeout_minutes: int) -> subprocess.CompletedProcess:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in _CONTROLLER_CREDENTIALS
        }
        environment["GIT_TERMINAL_PROMPT"] = "0"
        kwargs = dict(
            cwd=cwd,
            timeout=timeout_minutes * 60,
            capture_output=True,
            text=True,
            env=environment,
        )
        if self.on_progress is None:
            return self._runner(argv, **kwargs)
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._runner, argv, **kwargs)
            while True:
                try:
                    return future.result(timeout=self.heartbeat_seconds)
                except FutureTimeout:
                    elapsed = int(time.monotonic() - start)
                    self.on_progress(
                        f"{self.name} still working ({elapsed // 60}m {elapsed % 60:02d}s elapsed)"
                    )

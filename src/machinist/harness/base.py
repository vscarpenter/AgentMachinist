"""Base class for coding-harness adapters.

An adapter's job is small on purpose: build the argv that runs its CLI
headlessly for the two phases. Subprocess mechanics, timeouts, and error
translation live here so adapters stay one screen long.
"""

from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from machinist.config import HarnessConfig
from machinist.process import (
    HARNESS_CREDENTIAL_ALLOWLIST,
    ProcessCancelledError,
    ProcessOutputLimitError,
    ProcessStartError,
    ProcessStragglerError,
    credential_reduced_environment,
    run_supervised,
)

Runner = Callable[..., subprocess.CompletedProcess]


class HarnessError(Exception):
    """A harness invocation failed or timed out."""


class HarnessCancelledError(HarnessError):
    """The operator cooperatively cancelled a supervised harness."""

    cancelled = True


@dataclass(frozen=True)
class HarnessCapabilities:
    """Controls the adapter actually requests, not security guarantees."""

    spec_repository_writes: str
    implementation_git_control: str = "prompt-and-postcondition"


class Harness(ABC):
    name: ClassVar[str]
    default_command: ClassVar[str]
    capabilities: ClassVar[HarnessCapabilities] = HarnessCapabilities("advisory")

    # Harness runs are silent and can last many minutes; a periodic progress
    # callback keeps callers (and humans) sure the process is alive.
    heartbeat_seconds: float = 30.0
    on_progress: Callable[[str], None] | None = None
    # Verification-gate commands the implementation phase may execute for its
    # own feedback loop; the controller sets this before implement(). Adapters
    # whose execute mode already permits command execution ignore it.
    allowed_commands: tuple[str, ...] = ()

    def __init__(self, config: HarnessConfig, runner: Runner = subprocess.run):
        self.config = config
        # The registry historically passed subprocess.run explicitly.  Treat
        # that default as a request for the supervised implementation while
        # preserving injected runners used by adapters and tests.
        self._uses_supervisor = runner is subprocess.run or runner is run_supervised
        self._runner = run_supervised if self._uses_supervisor else runner
        self.cancel_check: Callable[[], bool] | None = None

    @property
    def command(self) -> str:
        return self.config.command or self.default_command

    @abstractmethod
    def spec_argv(self, prompt: str) -> list[str]:
        """Argv that makes the harness read a prompt and print a spec to stdout."""

    @abstractmethod
    def implement_argv(self, prompt: str) -> list[str]:
        """Argv that makes the harness edit files headlessly per the prompt."""

    def version_argv(self) -> list[str]:
        """Read-only argv used by readiness diagnostics."""
        return [self.command, "--version"]

    def compatibility_argv(self, phase: str) -> list[str]:
        """Parse the configured Phase argv without starting a Harness run."""
        if phase == "spec":
            argv = self.spec_argv("machinist compatibility probe")
        elif phase == "execute":
            argv = self.implement_argv("machinist compatibility probe")
        else:
            raise ValueError(f"unknown harness phase: {phase}")
        return [*argv, "--help"]

    def authentication_argv(self) -> list[str] | None:
        """Return a read-only auth probe, or None when the CLI has no probe."""
        return None

    def authentication_ready(self, result: subprocess.CompletedProcess) -> bool:
        """Interpret the configured CLI's auth probe without exposing details."""
        return result.returncode == 0

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
        except ProcessStartError as exc:
            if isinstance(exc.cause, FileNotFoundError):
                raise HarnessError(
                    f"harness executable '{argv[0]}' not found; install it or set harness.command"
                ) from exc
            raise HarnessError(f"{self.name} could not start: {exc.cause}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HarnessError(
                f"{self.name} timed out after {timeout_minutes} minutes"
            ) from exc
        except ProcessCancelledError as exc:
            raise HarnessCancelledError(f"{self.name} was cancelled") from exc
        except ProcessOutputLimitError as exc:
            raise HarnessError(
                f"{self.name} produced too much {exc.stream}: {exc}"
            ) from exc
        except ProcessStragglerError as exc:
            raise HarnessError(
                f"{self.name} left background processes running after exit; "
                "they were terminated"
            ) from exc
        if result.returncode != 0:
            raise HarnessError(
                f"{self.name} exited with {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout

    def _run_with_heartbeat(
        self, argv: list[str], cwd: Path, timeout_minutes: int
    ) -> subprocess.CompletedProcess:
        environment = credential_reduced_environment(allow=HARNESS_CREDENTIAL_ALLOWLIST)
        kwargs = {
            "cwd": cwd,
            "timeout": timeout_minutes * 60,
            "capture_output": True,
            "text": True,
            "env": environment,
        }
        if self._uses_supervisor:
            return self._runner(
                argv,
                **kwargs,
                progress_callback=self._report_progress if self.on_progress else None,
                progress_interval=self.heartbeat_seconds,
                cancel_check=self.cancel_check,
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

    def _report_progress(self, elapsed_seconds: float) -> None:
        if self.on_progress is None:
            return
        elapsed = int(elapsed_seconds)
        self.on_progress(
            f"{self.name} still working ({elapsed // 60}m {elapsed % 60:02d}s elapsed)"
        )

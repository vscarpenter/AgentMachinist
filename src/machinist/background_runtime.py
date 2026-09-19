"""Disposable Docker execution for delegated Tasks; never falls back to the host.

The controller retains Git/forge credentials and logs. A container sees only one
standalone clone, an ephemeral home, and (for Harness calls only) selected model
credentials. Bridge networking permits egress; it is not a network allowlist.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machinist.process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    HARNESS_CREDENTIAL_ALLOWLIST,
    ProcessCancelledError,
    ProcessStartError,
    ProcessSupervisionError,
    ProcessTimeoutError,
    run_supervised,
)

Runner = Callable[..., subprocess.CompletedProcess]
_CONTROLLER_LABEL = "dev.agentmachinist.controller"
_DOCKER_ENVIRONMENT = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
    "XDG_RUNTIME_DIR",
    "TMPDIR",
)
_HOME = "/tmp/machinist-home"


@dataclass(frozen=True)
class ContainerReadiness:
    ready: bool
    detail: str


class ContainerRuntime:
    """Subprocess-shaped Harness/gate runners under a shared persisted deadline.

    ``deadline`` returns UTC epoch seconds, or None before a Task is admitted.
    ``cleanup_stale`` must only be called while holding the repository's worker
    claim. ``probe`` is read-only and never pulls an image or starts a container.
    The configured image must provide /bin/sh, mkdir and GNU /usr/bin/timeout.
    """

    def __init__(
        self,
        image: str,
        *,
        controller_root: Path,
        workshop_root: Path,
        network: str = "bridge",
        cpus: float = 2,
        memory: str = "4g",
        credential_names: Sequence[str] = (),
        runner: Runner = run_supervised,
        deadline: Callable[[], float | None] | None = None,
        clock: Callable[[], float] = time.time,
        cancel_check: Callable[[], bool] | None = None,
    ):
        if not image or image.startswith("-") or any(c.isspace() for c in image):
            raise ValueError("a valid prebuilt container image is required")
        if network not in {"bridge", "none"}:
            raise ValueError("container network must be bridge or none")
        if not math.isfinite(cpus) or cpus <= 0:
            raise ValueError("container CPU limit must be positive")
        if not re.fullmatch(r"[1-9][0-9]*[kKmMgG]", memory):
            raise ValueError("container memory must be a positive k, m, or g quantity")
        if not set(credential_names) <= HARNESS_CREDENTIAL_ALLOWLIST:
            raise ValueError("only supported model credentials may enter a container")
        self.image = image
        self.controller_root = controller_root.expanduser().resolve()
        self.workshop_root = workshop_root.expanduser().resolve()
        if self.workshop_root.is_relative_to(self.controller_root):
            raise ValueError(
                "background Workshops must be outside the controller checkout"
            )
        self.network = network
        self.cpus = cpus
        self.memory = memory
        self.credential_names = tuple(credential_names)
        self._runner = runner
        self.deadline = deadline
        self.clock = clock
        self.cancel_check = cancel_check
        identity = f"{self.controller_root}\0{self.workshop_root}".encode()
        self._owner = hashlib.sha256(identity).hexdigest()[:24]

    def harness_runner(
        self, command: str | Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess:
        return self._run(command, provider_credentials=True, **kwargs)

    def gate_runner(
        self, command: str | Sequence[str], **kwargs: Any
    ) -> subprocess.CompletedProcess:
        return self._run(command, provider_credentials=False, **kwargs)

    def probe(self) -> ContainerReadiness:
        """Read daemon and local image readiness without altering either."""
        for command, unavailable in (
            (
                ["docker", "info", "--format", "{{.OSType}}"],
                "Docker daemon is unavailable",
            ),
            (
                ["docker", "image", "inspect", self.image],
                "Configured Docker image is not available locally; build or pull it first",
            ),
        ):
            try:
                result = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=_docker_environment(),
                    max_output_bytes=64 * 1024,
                )
            except (OSError, subprocess.SubprocessError, ProcessSupervisionError):
                return ContainerReadiness(False, unavailable)
            if result.returncode:
                return ContainerReadiness(False, unavailable)
        return ContainerReadiness(
            True, "Docker daemon and configured local image are available"
        )

    def cleanup_stale(self) -> None:
        """Reap abandoned containers for this worker after its exclusive claim."""
        result = self._runner(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={_CONTROLLER_LABEL}={self._owner}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=_docker_environment(),
            max_output_bytes=64 * 1024,
        )
        if result.returncode:
            raise ProcessStartError(
                "docker", ValueError("could not inspect stale background containers")
            )
        for identifier in (result.stdout or "").split():
            if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
                raise ProcessStartError(
                    "docker", ValueError("invalid stale container identity")
                )
            self._remove(identifier)

    def _workshop(self, cwd: str | os.PathLike[str] | None) -> Path:
        if cwd is None:
            raise ValueError("a clone Workshop cwd is required")
        raw = Path(cwd).expanduser().absolute()
        workshop = raw.resolve(strict=True)
        if raw != workshop:
            raise ValueError("Workshop path may not traverse symlinks")
        if (
            workshop == self.workshop_root
            or not workshop.is_relative_to(self.workshop_root)
            or workshop.is_relative_to(self.controller_root)
            or self.controller_root.is_relative_to(workshop)
        ):
            raise ValueError(
                "Workshop must be inside its dedicated root and outside controller state"
            )
        if any(c in str(workshop) for c in (",", "\n", "\r", '"')):
            raise ValueError("Workshop path contains unsupported mount characters")
        git = workshop / ".git"
        if git.is_symlink() or not git.is_dir() or not (git / "HEAD").is_file():
            raise ValueError(
                "background execution requires a standalone clone, not a worktree"
            )
        if (git / "commondir").exists() or (
            git / "objects" / "info" / "alternates"
        ).exists():
            raise ValueError(
                "clone Git metadata cannot use shared storage or alternates"
            )
        for path in git.rglob("*"):
            if path.is_symlink():
                raise ValueError("clone Git metadata cannot contain symlinks")
        return workshop

    def _run(
        self,
        command: str | Sequence[str],
        *,
        provider_credentials: bool,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
        timeout: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
        read_only_workshop: bool = False,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        def cancelled() -> bool:
            return bool(
                (self.cancel_check and self.cancel_check())
                or (cancel_check and cancel_check())
            )

        if cancelled():
            raise ProcessCancelledError(command)
        budget = 1800.0 if timeout is None else float(timeout)
        deadline = self.deadline() if self.deadline else None
        if deadline is not None:
            budget = min(budget, deadline - self.clock())
        if budget <= 0:
            raise ProcessTimeoutError(command, max(0.0, budget))
        if not math.isfinite(budget):
            raise ValueError("container timeout must be finite")
        try:
            workshop = self._workshop(cwd)
        except (OSError, ValueError) as exc:
            raise ProcessStartError(command, exc) from exc
        if shell:
            if not isinstance(command, str):
                raise ValueError("shell container commands must be strings")
            child_argv = ["/bin/sh", "-c", command]
        else:
            child_argv = [command] if isinstance(command, str) else list(command)
        if not child_argv:
            raise ValueError("container command cannot be empty")

        name = f"machinist-{self._owner}-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{_CONTROLLER_LABEL}={self._owner}",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "256",
            "--cpus",
            str(self.cpus),
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--network",
            self.network,
            "--no-healthcheck",
            "--log-driver",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=1g,mode=1777",
            "--mount",
            f"type=bind,src={workshop},dst={workshop}"
            + (",readonly" if read_only_workshop else ""),
            "--workdir",
            str(workshop),
            "--env",
            f"HOME={_HOME}",
            "--env",
            f"CODEX_HOME={_HOME}/.codex",
            "--env",
            "GIT_TERMINAL_PROMPT=0",
            "--env",
            "UV_LINK_MODE=copy",
            "--env",
            "CI=1",
        ]
        environment = _docker_environment()
        if provider_credentials:
            source = os.environ if env is None else env
            for key in self.credential_names:
                if source.get(key):
                    environment[key] = source[key]
                    argv.extend(["--env", key])
            if "OPENAI_API_KEY" in environment and "CODEX_API_KEY" not in environment:
                environment["CODEX_API_KEY"] = environment["OPENAI_API_KEY"]
                argv.extend(["--env", "CODEX_API_KEY"])
        argv.extend(
            [
                "--entrypoint",
                "/bin/sh",
                self.image,
                "-c",
                'mkdir -p "$HOME" && exec "$@"',
                "machinist",
                "/usr/bin/timeout",
                "--kill-after=5s",
                f"{budget:g}s",
                *child_argv,
            ]
        )
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
        cleanup = True
        try:
            result = self._runner(
                argv,
                cwd=self.controller_root,
                env=environment,
                shell=False,
                timeout=budget,
                cancel_check=cancelled,
                **kwargs,
            )
            if result.returncode == 124:
                raise ProcessTimeoutError(
                    command, budget, output=result.stdout, stderr=result.stderr
                )
            return subprocess.CompletedProcess(
                command, result.returncode, result.stdout, result.stderr
            )
        except FileNotFoundError:
            cleanup = False  # No Docker client means no container was submitted.
            raise
        finally:
            if cleanup:
                self._remove(name)

    def _remove(self, name: str) -> None:
        try:
            result = self._runner(
                ["docker", "rm", "--force", name],
                capture_output=True,
                text=True,
                timeout=10,
                env=_docker_environment(),
                max_output_bytes=64 * 1024,
            )
        except (OSError, subprocess.SubprocessError, ProcessSupervisionError) as exc:
            raise ProcessStartError(
                "docker",
                ValueError(f"container cleanup could not be confirmed for {name}"),
            ) from exc
        if result.returncode and "No such container" not in (result.stderr or ""):
            raise ProcessStartError(
                "docker",
                ValueError(f"container cleanup could not be confirmed for {name}"),
            )


def _docker_environment() -> dict[str, str]:
    """Only Docker's local connection settings; never forwarded wholesale."""
    return {
        name: os.environ[name] for name in _DOCKER_ENVIRONMENT if name in os.environ
    }

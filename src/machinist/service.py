"""Safe, per-repository launchd service management for macOS.

Importing this module has no process side effects.  A ``LaunchdService`` only
invokes ``launchctl`` when an explicit lifecycle method is called, and every
invocation uses an argv sequence through an injectable runner.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    reserve_regular_file,
    validate_regular_file,
)

Runner = Callable[..., subprocess.CompletedProcess]

_LABEL_PREFIX = "io.github.vscarpenter.agentmachinist.watch"
_LABEL_SAFE = re.compile(r"[^a-z0-9]+")
_COMMAND_TIMEOUT_SECONDS = 15
_MAX_DIAGNOSTIC_CHARS = 2_000
LOG_TAIL_READ_LIMIT_BYTES = 64 * 1024
LOG_TAIL_OUTPUT_LIMIT_BYTES = 64 * 1024
LOG_TAIL_TRUNCATION_MARKER = "[log truncated; showing bounded tail]"
_MISSING_SERVICE_MESSAGES = (
    "could not find service",
    "no such process",
    "service not found",
)


class ServiceError(Exception):
    """A service definition or launchd operation is unsafe or failed."""


class ServiceCommandError(ServiceError):
    """A launchctl command returned a non-zero status."""

    def __init__(self, command: ServiceCommand):
        self.argv = command.argv
        self.returncode = command.returncode
        self.stdout = command.stdout
        self.stderr = command.stderr
        diagnostic = _diagnostic(command)
        action = command.argv[1] if len(command.argv) > 1 else "command"
        super().__init__(
            f"launchctl {action} failed with exit code {command.returncode}: "
            f"{diagnostic}"
        )


@dataclass(frozen=True)
class ServiceCommand:
    """Captured result of one bounded launchctl invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ServiceStatus:
    """Registration status plus raw launchctl output for human diagnostics."""

    label: str
    installed: bool
    loaded: bool
    returncode: int
    output: str = ""
    error: str | None = None


@dataclass(frozen=True)
class LogTail:
    """A bounded, decoded suffix of one service log."""

    text: str
    truncated: bool
    bytes_read: int


def service_identifier(repo_root: str | Path) -> str:
    """Return a bounded launchd label unique to a canonical repository path."""
    repository = _repository_root(repo_root)
    slug = _LABEL_SAFE.sub("-", repository.name.casefold()).strip("-") or "repo"
    digest = hashlib.sha256(os.fsencode(str(repository))).hexdigest()[:12]
    max_slug_length = 127 - len(_LABEL_PREFIX) - len(digest) - 2
    slug = slug[:max_slug_length].rstrip("-") or "repo"
    return f"{_LABEL_PREFIX}.{slug}-{digest}"


def read_log_tail(
    path: str | Path,
    *,
    lines: int,
    read_limit_bytes: int = LOG_TAIL_READ_LIMIT_BYTES,
    output_limit_bytes: int = LOG_TAIL_OUTPUT_LIMIT_BYTES,
) -> LogTail:
    """Read only a bounded file suffix and return at most ``lines`` data lines.

    The truncation marker is included in the output byte budget. Invalid UTF-8
    is replaced so a partially read multibyte character or a damaged log never
    makes the recovery command fail.
    """
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
        raise ServiceError("log tail line count must be a positive integer")
    if (
        isinstance(read_limit_bytes, bool)
        or not isinstance(read_limit_bytes, int)
        or read_limit_bytes < 1
    ):
        raise ServiceError("log tail read limit must be a positive integer")
    marker_bytes = LOG_TAIL_TRUNCATION_MARKER.encode("utf-8")
    if (
        isinstance(output_limit_bytes, bool)
        or not isinstance(output_limit_bytes, int)
        or output_limit_bytes < len(marker_bytes)
    ):
        raise ServiceError("log tail output limit must fit the truncation marker")

    log_path = Path(path)
    flags = os.O_RDONLY
    for optional_flag in ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
        flags |= getattr(os, optional_flag, 0)
    descriptor = os.open(log_path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ServiceError(f"service log is not a regular file: {log_path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            offset = max(0, size - read_limit_bytes)
            stream.seek(offset)
            raw = stream.read(read_limit_bytes)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    decoded = raw.decode("utf-8", errors="replace")
    candidates = decoded.splitlines()
    line_truncated = len(candidates) > lines
    content = "\n".join(candidates[-lines:])
    content_bytes = content.encode("utf-8")
    truncated = offset > 0 or line_truncated or len(content_bytes) > output_limit_bytes
    if not truncated:
        return LogTail(text=content, truncated=False, bytes_read=len(raw))

    remaining = output_limit_bytes - len(marker_bytes)
    if not content or remaining < 2:
        text = LOG_TAIL_TRUNCATION_MARKER
    else:
        bounded = _utf8_suffix(content, remaining - 1)
        text = f"{LOG_TAIL_TRUNCATION_MARKER}\n{bounded}"
    return LogTail(text=text, truncated=True, bytes_read=len(raw))


class LaunchdService:
    """Build and manage one user's scheduled AgentMachinist watcher."""

    def __init__(
        self,
        repo_root: str | Path,
        executable: str | Path,
        *,
        launch_agents_dir: str | Path | None = None,
        logs_dir: str | Path | None = None,
        arguments: Sequence[str] = ("watch", "--once"),
        start_interval: int = 60,
        exit_timeout: int = 30,
        path_environment: str | None = None,
        uid: int | None = None,
        launchctl_path: str | Path = "/bin/launchctl",
        runner: Runner = subprocess.run,
    ):
        self._configure(
            repo_root,
            executable=_executable_path(executable),
            launch_agents_dir=launch_agents_dir,
            logs_dir=logs_dir,
            arguments=arguments,
            start_interval=start_interval,
            exit_timeout=exit_timeout,
            path_environment=path_environment,
            uid=uid,
            launchctl_path=launchctl_path,
            runner=runner,
        )

    @classmethod
    def for_management(
        cls,
        repo_root: str | Path,
        *,
        launch_agents_dir: str | Path | None = None,
        logs_dir: str | Path | None = None,
        uid: int | None = None,
        launchctl_path: str | Path = "/bin/launchctl",
        runner: Runner = subprocess.run,
    ) -> LaunchdService:
        """Open an installed service without needing its executable or config.

        launchd lifecycle operations address a service by its repository-derived
        label and installed plist.  Keeping that management path independent of
        the current controller installation makes recovery commands usable when
        the repository config is broken or ``machinist`` has left ``PATH``.
        This object intentionally cannot render or install a replacement plist.
        """
        service = cls.__new__(cls)
        service._configure(
            repo_root,
            executable=None,
            launch_agents_dir=launch_agents_dir,
            logs_dir=logs_dir,
            arguments=("watch", "--once"),
            start_interval=60,
            exit_timeout=30,
            path_environment=None,
            uid=uid,
            launchctl_path=launchctl_path,
            runner=runner,
        )
        return service

    def _configure(
        self,
        repo_root: str | Path,
        *,
        executable: Path | None,
        launch_agents_dir: str | Path | None,
        logs_dir: str | Path | None,
        arguments: Sequence[str],
        start_interval: int,
        exit_timeout: int,
        path_environment: str | None,
        uid: int | None,
        launchctl_path: str | Path,
        runner: Runner,
    ) -> None:
        self.repo_root = _repository_root(repo_root)
        self.executable = executable
        self.arguments = _arguments(arguments)
        self.start_interval = _seconds(
            start_interval,
            minimum=10,
            description="start interval",
        )
        self.exit_timeout = _seconds(
            exit_timeout,
            minimum=0,
            description="exit timeout",
        )
        self.path_environment = _path_environment(path_environment)

        resolved_uid = os.getuid() if uid is None else uid
        if (
            isinstance(resolved_uid, bool)
            or not isinstance(resolved_uid, int)
            or resolved_uid < 1
        ):
            raise ServiceError("a positive non-root user ID is required")
        self.uid = resolved_uid
        self.domain = f"gui/{self.uid}"

        raw_launch_agents = (
            Path.home() / "Library" / "LaunchAgents"
            if launch_agents_dir is None
            else Path(launch_agents_dir).expanduser()
        )
        if not raw_launch_agents.is_absolute():
            raise ServiceError("LaunchAgents directory must be absolute")
        self.launch_agents_dir = raw_launch_agents.resolve(strict=False)

        self.label = service_identifier(self.repo_root)
        self.plist_path = self.launch_agents_dir / f"{self.label}.plist"
        _require_direct_child(
            self.plist_path,
            self.launch_agents_dir,
            description="service plist",
        )

        raw_logs = (
            self.repo_root / ".machinist" / "runs" / "service"
            if logs_dir is None
            else Path(logs_dir).expanduser()
        )
        if not raw_logs.is_absolute():
            raw_logs = self.repo_root / raw_logs
        self._logs_parts: tuple[str, ...]
        try:
            if logs_dir is None:
                self._logs_runtime = RuntimeDirectory.bind(
                    self.repo_root / ".machinist" / "runs",
                    repo_root=self.repo_root,
                )
                self._logs_parts = ("service",)
                self.logs_dir = self._logs_runtime.subdirectory(
                    *self._logs_parts, create=False
                )
            else:
                self._logs_runtime = RuntimeDirectory.bind(
                    raw_logs,
                    repo_root=self.repo_root,
                )
                self._logs_parts = ()
                self.logs_dir = self._logs_runtime.path
        except RuntimePathError as exc:
            raise ServiceError(
                "logs directory must be contained by repository and contain no "
                f"symlinks: {exc}"
            ) from exc
        self.stdout_log_path = self.logs_dir / "watch.stdout.log"
        self.stderr_log_path = self.logs_dir / "watch.stderr.log"
        self._validate_log_files()

        launchctl = Path(launchctl_path).expanduser()
        if not launchctl.is_absolute():
            raise ServiceError("launchctl path must be absolute")
        self.launchctl_path = launchctl.resolve(strict=False)
        self._runner = runner

    @property
    def service_target(self) -> str:
        return f"{self.domain}/{self.label}"

    @property
    def log_paths(self) -> tuple[Path, Path]:
        return self.stdout_log_path, self.stderr_log_path

    @property
    def installed(self) -> bool:
        return (
            not self.plist_path.is_symlink()
            and self.plist_path.exists()
            and self.plist_path.is_file()
        )

    def plist_payload(self) -> dict[str, Any]:
        """Return the structured launchd definition without touching disk."""
        if self.executable is None:
            raise ServiceError(
                "a management-only service cannot render or install a plist; "
                "provide the current controller executable"
            )
        self._validate_managed_paths()
        environment = {
            "GH_PROMPT_DISABLED": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONUNBUFFERED": "1",
        }
        if self.path_environment is not None:
            environment["PATH"] = self.path_environment
        return {
            "Label": self.label,
            "ProgramArguments": [str(self.executable), *self.arguments],
            "WorkingDirectory": str(self.repo_root),
            "StartInterval": self.start_interval,
            "ExitTimeOut": self.exit_timeout,
            "StandardOutPath": str(self.stdout_log_path),
            "StandardErrorPath": str(self.stderr_log_path),
            "EnvironmentVariables": environment,
            "Umask": 0o077,
        }

    def plist_bytes(self) -> bytes:
        """Serialize the plist with XML escaping handled by ``plistlib``."""
        try:
            return plistlib.dumps(
                self.plist_payload(),
                fmt=plistlib.FMT_XML,
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ServiceError(f"could not serialize service plist: {exc}") from exc

    def install(self) -> Path:
        """Atomically install or replace the per-repository LaunchAgent plist."""
        if self.executable is None:
            raise ServiceError(
                "a management-only service cannot install a plist; "
                "provide the current controller executable"
            )
        self._validate_managed_paths()
        try:
            self.launch_agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._ensure_logs(create=True)
            reserve_regular_file(self.stdout_log_path)
            reserve_regular_file(self.stderr_log_path)
        except RuntimePathError as exc:
            raise ServiceError(f"service log file is unsafe: {exc}") from exc
        except OSError as exc:
            raise ServiceError(f"could not create service directories: {exc}") from exc
        self._validate_managed_paths()

        descriptor = -1
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.label}.",
                suffix=".tmp",
                dir=self.launch_agents_dir,
            )
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(self.plist_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.plist_path)
        except OSError as exc:
            raise ServiceError(f"could not install service plist: {exc}") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)
        return self.plist_path

    def bootstrap(self) -> ServiceCommand:
        """Register the installed plist in the user's GUI launchd domain."""
        if not self.installed:
            raise ServiceError(f"service plist is not installed at {self.plist_path}")
        return self._launchctl(
            "bootstrap",
            self.domain,
            str(self.plist_path),
            check=True,
        )

    def start(self) -> ServiceCommand:
        """Immediately start a registered service without forcing a restart."""
        return self._launchctl("kickstart", self.service_target, check=True)

    def restart(self) -> ServiceCommand:
        """Stop and immediately restart a registered service instance."""
        return self._launchctl("kickstart", "-k", self.service_target, check=True)

    def bootout(self) -> ServiceCommand:
        """Strictly remove the service from its launchd domain."""
        return self._launchctl("bootout", self.service_target, check=True)

    def stop(self) -> ServiceCommand:
        """Idempotently remove the service from its launchd domain."""
        command = self._launchctl("bootout", self.service_target, check=False)
        if not command.succeeded and not _service_is_missing(command):
            raise ServiceCommandError(command)
        return command

    def status(self) -> ServiceStatus:
        """Use launchctl print's exit status; retain its text only for humans."""
        command = self._launchctl("print", self.service_target, check=False)
        return ServiceStatus(
            label=self.label,
            installed=self.installed,
            loaded=command.succeeded,
            returncode=command.returncode,
            output=command.stdout,
            error=None if command.succeeded else _diagnostic(command),
        )

    def print_status(self) -> ServiceStatus:
        """Named alias matching launchctl's ``print`` terminology."""
        return self.status()

    def uninstall(self) -> bool:
        """Boot out first, then remove only the managed plist; retain logs."""
        self.stop()
        self._validate_managed_paths()
        present = self.plist_path.exists() or self.plist_path.is_symlink()
        if not present:
            return False
        try:
            self.plist_path.unlink()
        except OSError as exc:
            raise ServiceError(f"could not remove service plist: {exc}") from exc
        return True

    def _validate_managed_paths(self) -> None:
        _require_direct_child(
            self.plist_path,
            self.launch_agents_dir,
            description="service plist",
        )
        self._ensure_logs(create=False)
        self._validate_log_files()

    def _validate_log_files(self) -> None:
        try:
            validate_regular_file(self.stdout_log_path)
            validate_regular_file(self.stderr_log_path)
        except RuntimePathError as exc:
            raise ServiceError(f"service log file is unsafe: {exc}") from exc

    def _ensure_logs(self, *, create: bool) -> Path:
        try:
            if self._logs_parts:
                return self._logs_runtime.subdirectory(*self._logs_parts, create=create)
            return self._logs_runtime.ensure(create=create)
        except RuntimePathError as exc:
            raise ServiceError(f"logs directory is unsafe: {exc}") from exc

    def _launchctl(self, *arguments: str, check: bool) -> ServiceCommand:
        argv = (str(self.launchctl_path), *arguments)
        try:
            result = self._runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            action = arguments[0] if arguments else "command"
            raise ServiceError(f"could not run launchctl {action}: {exc}") from exc

        command = ServiceCommand(
            argv=argv,
            returncode=int(result.returncode),
            stdout=_text(result.stdout),
            stderr=_text(result.stderr),
        )
        if check and not command.succeeded:
            raise ServiceCommandError(command)
        return command


def _repository_root(repo_root: str | Path) -> Path:
    raw = Path(repo_root).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ServiceError(f"repository root does not exist: {raw}") from exc
    if not resolved.is_dir():
        raise ServiceError(f"repository root is not a directory: {resolved}")
    return resolved


def _executable_path(executable: str | Path) -> Path:
    raw = Path(executable).expanduser()
    if not raw.is_absolute():
        raise ServiceError("controller executable path must be absolute")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ServiceError(f"controller executable file does not exist: {raw}") from exc
    if not resolved.is_file():
        raise ServiceError(
            f"controller executable is not an executable file: {resolved}"
        )
    if not os.access(resolved, os.X_OK):
        raise ServiceError(f"controller executable is not executable: {resolved}")
    return resolved


def _arguments(arguments: Sequence[str]) -> tuple[str, ...]:
    if isinstance(arguments, (str, bytes)):
        raise ServiceError("service arguments must be a sequence of argument strings")
    result = tuple(arguments)
    if not result:
        raise ServiceError("service arguments cannot be empty")
    for argument in result:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ServiceError("service arguments must be strings without NUL bytes")
    return result


def _seconds(value: int, *, minimum: int, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        if description == "start interval":
            raise ServiceError("start interval must be at least 10 seconds")
        raise ServiceError(f"{description} must be at least {minimum} seconds")
    return value


def _path_environment(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ServiceError("PATH must be a non-empty string without NUL bytes")
    components = value.split(os.pathsep)
    if any(
        not component or not Path(component).is_absolute() for component in components
    ):
        raise ServiceError("PATH entries persisted for launchd must all be absolute")
    return value


def _require_direct_child(path: Path, parent: Path, *, description: str) -> None:
    try:
        resolved_parent = path.parent.resolve(strict=False)
    except OSError as exc:
        raise ServiceError(f"could not resolve {description} parent: {exc}") from exc
    if resolved_parent != parent:
        raise ServiceError(f"{description} must be contained by {parent}")


def _require_descendant(path: Path, root: Path, *, description: str) -> None:
    if path == root or root not in path.parents:
        raise ServiceError(f"{description} must be contained by repository {root}")


def _service_is_missing(command: ServiceCommand) -> bool:
    diagnostic = f"{command.stdout}\n{command.stderr}".casefold()
    return any(message in diagnostic for message in _MISSING_SERVICE_MESSAGES)


def _diagnostic(command: ServiceCommand) -> str:
    value = (command.stderr or command.stdout).strip()
    if not value:
        value = "no diagnostic output"
    if len(value) > _MAX_DIAGNOSTIC_CHARS:
        return value[:_MAX_DIAGNOSTIC_CHARS] + "…"
    return value


def _utf8_suffix(value: str, limit: int) -> str:
    """Return a valid UTF-8 suffix whose encoded size is at most ``limit``."""
    if limit <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = encoded[-limit:]
    while suffix and suffix[0] & 0xC0 == 0x80:
        suffix = suffix[1:]
    return suffix.decode("utf-8")


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)

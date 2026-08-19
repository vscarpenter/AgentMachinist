"""Credential-reduced, process-group-aware subprocess supervision.

This module intentionally exposes a small ``subprocess.run``-shaped API so
Harnesses and quality gates can share the same environment and termination
policy.  It reduces credentials passed through the environment; it is not an
operating-system sandbox and cannot hide credentials stored in files.
"""

from __future__ import annotations

import locale
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import IO, Any

DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_TERMINATION_GRACE_SECONDS = 2.0

# Provider credentials are needed by the supported coding Harnesses.  They are
# explicit exceptions to the generic secret-name filter; controller and cloud
# credentials remain denied.
HARNESS_CREDENTIAL_ALLOWLIST = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "CODEX_API_KEY",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    }
)

_DENIED_EXACT_NAMES = frozenset(
    {
        "CI_JOB_TOKEN",
        "CF_API_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "DIGITALOCEAN_ACCESS_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "KUBECONFIG",
        "NPM_TOKEN",
        "PYPI_API_TOKEN",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
        "SSH_AUTH_SOCK",
        "TWINE_PASSWORD",
        "VAULT_TOKEN",
    }
)
_DENIED_PREFIXES = (
    "ARM_",  # Terraform's Azure provider
    "AWS_",
    "AZURE_",
    "CLOUDSDK_",
    "GCP_",
    "GCLOUD_",
    "GOOGLE_CLOUD_",
    "OCI_",
    "SSH_",
)
_SECRET_SUFFIXES = (
    "_ACCESS_KEY",
    "_API_KEY",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_SECRET",
    "_TOKEN",
)


class ProcessSupervisionError(Exception):
    """Base class for stable process-supervision failures."""


class ProcessStartError(ProcessSupervisionError):
    """The child process could not be started."""

    def __init__(self, command: str | Sequence[str], cause: OSError):
        self.command = command
        self.cause = cause
        executable = command if isinstance(command, str) else command[0]
        super().__init__(f"could not start process '{executable}': {cause}")


class ProcessTimeoutError(subprocess.TimeoutExpired, ProcessSupervisionError):
    """The process group exceeded its deadline and was terminated."""


class ProcessCancelledError(ProcessSupervisionError):
    """The caller cancelled the process group."""

    def __init__(
        self, command: str | Sequence[str], stdout: Any = None, stderr: Any = None
    ):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        super().__init__("process was cancelled")


class ProcessOutputLimitError(ProcessSupervisionError):
    """A captured stream exceeded the configured byte limit."""

    def __init__(self, stream: str, limit: int):
        self.stream = stream
        self.limit = limit
        super().__init__(f"process {stream} exceeded the {limit}-byte capture limit")


def credential_reduced_environment(
    source: Mapping[str, str] | None = None,
    *,
    allow: Iterable[str] = (),
    deny: Iterable[str] = (),
) -> dict[str, str]:
    """Return an environment with controller/cloud credentials removed.

    Ordinary execution context such as ``PATH``, ``HOME``, locale variables,
    and temporary-directory settings is preserved.  ``allow`` is an explicit
    exception list for credentials a particular child genuinely needs.
    """

    environment = os.environ if source is None else source
    allowed = {name.upper() for name in allow}
    denied = _DENIED_EXACT_NAMES | {name.upper() for name in deny}
    reduced: dict[str, str] = {}
    for name, value in environment.items():
        upper = name.upper()
        if upper in allowed:
            reduced[name] = value
            continue
        if upper in denied or upper.startswith(_DENIED_PREFIXES):
            continue
        if upper in {"ACCESS_TOKEN", "API_KEY", "PASSWORD", "SECRET", "TOKEN"}:
            continue
        if upper.endswith(_SECRET_SUFFIXES):
            continue
        reduced[name] = value

    # Git subprocesses launched by a Harness or test command must fail rather
    # than pausing indefinitely for interactive credentials.
    reduced["GIT_TERMINAL_PROMPT"] = "0"
    return reduced


def run_supervised(
    command: str | Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    shell: bool = False,
    timeout: float | None = None,
    capture_output: bool = True,
    text: bool = False,
    encoding: str | None = None,
    errors: str | None = None,
    check: bool = False,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    stdout_log: str | os.PathLike[str] | None = None,
    stderr_log: str | os.PathLike[str] | None = None,
    progress_callback: Callable[[float], None] | None = None,
    progress_interval: float = 30.0,
    cancel_check: Callable[[], bool] | None = None,
    termination_grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
    poll_interval: float = 0.05,
) -> subprocess.CompletedProcess:
    """Run a child in its own process group and supervise its whole lifetime.

    Captured streams are written to files while the process runs, keeping
    memory bounded.  A stream larger than ``max_output_bytes`` fails loudly
    instead of silently truncating a Harness-generated specification.  Optional
    log paths retain the captured streams after this call returns.

    The accepted arguments intentionally cover the subset used by the Harness
    and implementation test gate, making this callable usable as their runner.
    """

    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    if progress_callback is not None and progress_interval <= 0:
        raise ValueError("progress_interval must be positive")
    if termination_grace_seconds < 0:
        raise ValueError("termination_grace_seconds must be non-negative")

    child_environment = credential_reduced_environment() if env is None else dict(env)
    started_at = time.monotonic()

    with ExitStack() as stack:
        temporary = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="machinist-process-")
        )
        stdout_path = _stream_path(stdout_log, Path(temporary) / "stdout.log")
        stderr_path = _stream_path(stderr_log, Path(temporary) / "stderr.log")
        stdout_handle = (
            _open_stream(stack, stdout_path) if capture_output or stdout_log else None
        )
        stderr_handle = (
            _open_stream(stack, stderr_path) if capture_output or stderr_log else None
        )

        popen_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": child_environment,
            "shell": shell,
            "stdin": subprocess.DEVNULL,
            "stdout": stdout_handle,
            "stderr": stderr_handle,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover - project CI is macOS/Linux
            popen_kwargs["creationflags"] = subprocess.__dict__[
                "CREATE_NEW_PROCESS_GROUP"
            ]

        try:
            process = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            raise ProcessStartError(command, exc) from exc

        deadline = None if timeout is None else started_at + timeout
        next_progress = (
            started_at + progress_interval if progress_callback is not None else None
        )
        group_terminated = False
        try:
            while process.poll() is None:
                oversized = _oversized_stream(
                    stdout_handle,
                    stderr_handle,
                    max_output_bytes=max_output_bytes,
                )
                if oversized is not None:
                    _terminate_process_group(process, termination_grace_seconds)
                    group_terminated = True
                    raise ProcessOutputLimitError(oversized, max_output_bytes)

                if cancel_check is not None and cancel_check():
                    _terminate_process_group(process, termination_grace_seconds)
                    group_terminated = True
                    stdout, stderr = _read_streams(
                        stdout_handle,
                        stderr_handle,
                        text=text,
                        encoding=encoding,
                        errors=errors,
                        max_output_bytes=max_output_bytes,
                    )
                    raise ProcessCancelledError(command, stdout=stdout, stderr=stderr)

                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    assert timeout is not None
                    _terminate_process_group(process, termination_grace_seconds)
                    group_terminated = True
                    stdout, stderr = _read_streams(
                        stdout_handle,
                        stderr_handle,
                        text=text,
                        encoding=encoding,
                        errors=errors,
                        max_output_bytes=max_output_bytes,
                    )
                    raise ProcessTimeoutError(
                        command,
                        timeout,
                        output=stdout,
                        stderr=stderr,
                    )

                if (
                    progress_callback is not None
                    and next_progress is not None
                    and now >= next_progress
                ):
                    progress_callback(now - started_at)
                    next_progress = now + progress_interval

                time.sleep(poll_interval)
        except BaseException:
            # KeyboardInterrupt and progress-callback failures must receive the
            # same cleanup guarantee as explicit cancellation and timeouts.
            if not group_terminated:
                _terminate_process_group(process, termination_grace_seconds)
            raise

        oversized = _oversized_stream(
            stdout_handle,
            stderr_handle,
            max_output_bytes=max_output_bytes,
        )
        if oversized is not None:
            _terminate_process_group(process, termination_grace_seconds)
            raise ProcessOutputLimitError(oversized, max_output_bytes)

        stdout, stderr = _read_streams(
            stdout_handle,
            stderr_handle,
            text=text,
            encoding=encoding,
            errors=errors,
            max_output_bytes=max_output_bytes,
        )
        completed = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
        if check:
            completed.check_returncode()
        return completed


def _stream_path(configured: str | os.PathLike[str] | None, fallback: Path) -> Path:
    path = fallback if configured is None else Path(configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _open_stream(stack: ExitStack, path: Path) -> IO[bytes]:
    return stack.enter_context(path.open("w+b"))


def _oversized_stream(
    stdout: IO[bytes] | None,
    stderr: IO[bytes] | None,
    *,
    max_output_bytes: int,
) -> str | None:
    for name, stream in (("stdout", stdout), ("stderr", stderr)):
        if stream is not None and os.fstat(stream.fileno()).st_size > max_output_bytes:
            return name
    return None


def _read_streams(
    stdout: IO[bytes] | None,
    stderr: IO[bytes] | None,
    *,
    text: bool,
    encoding: str | None,
    errors: str | None,
    max_output_bytes: int,
) -> tuple[str | bytes | None, str | bytes | None]:
    return (
        _read_stream(
            stdout,
            text=text,
            encoding=encoding,
            errors=errors,
            max_output_bytes=max_output_bytes,
        ),
        _read_stream(
            stderr,
            text=text,
            encoding=encoding,
            errors=errors,
            max_output_bytes=max_output_bytes,
        ),
    )


def _read_stream(
    stream: IO[bytes] | None,
    *,
    text: bool,
    encoding: str | None,
    errors: str | None,
    max_output_bytes: int,
) -> str | bytes | None:
    if stream is None:
        return None
    stream.flush()
    stream.seek(0)
    payload = stream.read(max_output_bytes + 1)
    if len(payload) > max_output_bytes:
        raise ProcessOutputLimitError("output", max_output_bytes)
    if not text:
        return payload
    return payload.decode(
        encoding or locale.getpreferredencoding(False),
        errors=errors or "strict",
    )


def _terminate_process_group(process: subprocess.Popen, grace_seconds: float) -> None:
    if os.name == "posix":
        group_id = process.pid
        _signal_posix_group(group_id, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline and _posix_group_exists(group_id):
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        if _posix_group_exists(group_id):
            _signal_posix_group(group_id, signal.SIGKILL)
    else:  # pragma: no cover - project CI is macOS/Linux
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
    try:
        process.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:  # pragma: no cover - kill should be final
        process.kill()
        process.wait()


def _signal_posix_group(group_id: int, sig: signal.Signals) -> None:
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        pass


def _posix_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # It exists, but the caller cannot signal it.
        return True
    return True

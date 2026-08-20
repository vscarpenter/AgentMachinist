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
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import ExitStack
from pathlib import Path
from types import FrameType
from typing import IO, Any

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    open_regular_file,
)

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

    def __init__(
        self,
        command: str | Sequence[str],
        cause: OSError | ValueError | RuntimePathError,
    ):
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


class ProcessStragglerError(ProcessSupervisionError):
    """The process leader exited while descendants remained in its group."""

    def __init__(
        self,
        command: str | Sequence[str],
        leader_returncode: int,
        stdout: Any = None,
        stderr: Any = None,
    ):
        self.command = command
        self.leader_returncode = leader_returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            "process leader exited with status "
            f"{leader_returncode} while process-group descendants were still "
            "running; descendants were terminated"
        )


class ProcessSignalInterruption(SystemExit):
    """A controller termination signal received during active supervision.

    This intentionally derives from ``SystemExit`` rather than ``Exception``:
    command loops must not mistake operator/service termination for an ordinary
    task failure and continue watching.  The numeric exit code preserves normal
    shell signal semantics without printing a traceback.
    """

    cancelled = True

    def __init__(
        self,
        command: str | Sequence[str],
        signal_number: int,
        stdout: Any = None,
        stderr: Any = None,
    ):
        self.command = command
        self.signal_number = signal_number
        self.signal_name = signal.Signals(signal_number).name
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(128 + signal_number)

    def __str__(self) -> str:
        return f"process interrupted by {self.signal_name}"


class _TerminationSignal(BaseException):
    def __init__(self, signal_number: int):
        self.signal_number = signal_number


class _ScopedTerminationSignals:
    """Temporarily turn POSIX service signals into supervised interruptions."""

    def __init__(self) -> None:
        self._received: int | None = None
        self._child_started = False
        self._cleaning = False
        self._prior_handlers: dict[signal.Signals, Any] = {}

    def __enter__(self) -> _ScopedTerminationSignals:
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
        ):
            return self
        for current_signal in (signal.SIGTERM, signal.SIGHUP):
            self._prior_handlers[current_signal] = signal.getsignal(current_signal)
            signal.signal(current_signal, self._handle)
        return self

    def __exit__(self, *_exc: object) -> None:
        for current_signal, prior_handler in self._prior_handlers.items():
            signal.signal(current_signal, prior_handler)

    def raise_if_received(self) -> None:
        if self._received is not None and not self._cleaning:
            raise _TerminationSignal(self._received)

    def child_started(self) -> None:
        self._child_started = True
        self.raise_if_received()

    def begin_cleanup(self) -> None:
        # Keep our handlers installed during bounded cleanup, but make repeated
        # TERM/HUP idempotent so they cannot interrupt process-group teardown.
        self._cleaning = True

    def _handle(self, signal_number: int, _frame: FrameType | None) -> None:
        if self._received is None:
            self._received = signal_number
        if self._child_started and not self._cleaning:
            raise _TerminationSignal(self._received)


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

    On POSIX, returning from this function also proves that the dedicated
    process group is empty.  If a leader exits while a background descendant
    survives, the group is terminated and ``ProcessStragglerError`` is raised
    even when the leader returned zero.  This fail-closed rule prevents an
    apparently successful command from retaining credentials or mutating the
    workspace after its evidence has been collected.

    In the POSIX main thread, ``SIGTERM`` and ``SIGHUP`` are scoped to the
    active call.  Either signal first terminates the child process group, then
    restores the caller's handlers and raises ``ProcessSignalInterruption``.
    That ``SystemExit`` subtype preserves service/shell exit semantics and is
    deliberately not caught by ordinary task-error or watch-loop handlers.
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
        # Install before preparing logs or starting the child.  Pre-start
        # signals are recorded (not raised from resource setup) and converted
        # at the guarded launch boundary below.
        signal_scope = stack.enter_context(_ScopedTerminationSignals())
        temporary = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="machinist-process-")
        )
        try:
            stdout_path = _stream_path(stdout_log, Path(temporary) / "stdout.log")
            stderr_path = _stream_path(stderr_log, Path(temporary) / "stderr.log")
            stdout_handle = (
                _open_stream(stack, stdout_path)
                if capture_output or stdout_log
                else None
            )
            stderr_handle = (
                _open_stream(stack, stderr_path)
                if capture_output or stderr_log
                else None
            )
        except (OSError, RuntimePathError) as exc:
            raise ProcessStartError(command, exc) from exc

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

        process: subprocess.Popen | None = None
        try:
            signal_scope.raise_if_received()
            try:
                process = subprocess.Popen(command, **popen_kwargs)
            except (OSError, ValueError) as exc:
                # A termination request that raced process startup takes
                # precedence over an ordinary start diagnostic.
                signal_scope.raise_if_received()
                raise ProcessStartError(command, exc) from exc
            signal_scope.child_started()
            return _supervise_started_process(
                process,
                command,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                started_at=started_at,
                timeout=timeout,
                text=text,
                encoding=encoding,
                errors=errors,
                check=check,
                max_output_bytes=max_output_bytes,
                progress_callback=progress_callback,
                progress_interval=progress_interval,
                cancel_check=cancel_check,
                termination_grace_seconds=termination_grace_seconds,
                poll_interval=poll_interval,
            )
        except _TerminationSignal as exc:
            signal_scope.begin_cleanup()
            if process is not None:
                _terminate_process_group(process, termination_grace_seconds)
            stdout, stderr = _read_interruption_streams(
                stdout_handle,
                stderr_handle,
                text=text,
                encoding=encoding,
                errors=errors,
                max_output_bytes=max_output_bytes,
            )
            raise ProcessSignalInterruption(
                command,
                exc.signal_number,
                stdout=stdout,
                stderr=stderr,
            ) from None


def _supervise_started_process(
    process: subprocess.Popen,
    command: str | Sequence[str],
    *,
    stdout_handle: IO[bytes] | None,
    stderr_handle: IO[bytes] | None,
    started_at: float,
    timeout: float | None,
    text: bool,
    encoding: str | None,
    errors: str | None,
    check: bool,
    max_output_bytes: int,
    progress_callback: Callable[[float], None] | None,
    progress_interval: float,
    cancel_check: Callable[[], bool] | None,
    termination_grace_seconds: float,
    poll_interval: float,
) -> subprocess.CompletedProcess:
    deadline = None if timeout is None else started_at + timeout
    next_progress = (
        started_at + progress_interval if progress_callback is not None else None
    )
    group_terminated = False
    try:
        while process.poll() is None:
            if cancel_check is not None and cancel_check():
                _terminate_process_group(process, termination_grace_seconds)
                group_terminated = True
                stdout, stderr = _read_interruption_streams(
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
                stdout, stderr = _read_interruption_streams(
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

            oversized = _oversized_stream(
                stdout_handle,
                stderr_handle,
                max_output_bytes=max_output_bytes,
            )
            if oversized is not None:
                _terminate_process_group(process, termination_grace_seconds)
                group_terminated = True
                raise ProcessOutputLimitError(oversized, max_output_bytes)

            if (
                progress_callback is not None
                and next_progress is not None
                and now >= next_progress
            ):
                progress_callback(now - started_at)
                next_progress = now + progress_interval

            time.sleep(poll_interval)
    except _TerminationSignal:
        # The outer scope keeps temporary TERM/HUP handlers installed while it
        # performs idempotent, bounded process-group cleanup.
        raise
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

    if os.name == "posix" and _posix_group_exists(process.pid):
        leader_returncode = process.returncode
        assert leader_returncode is not None
        _terminate_process_group(process, termination_grace_seconds)
        stdout, stderr = _read_streams(
            stdout_handle,
            stderr_handle,
            text=text,
            encoding=encoding,
            errors=errors,
            max_output_bytes=max_output_bytes,
        )
        raise ProcessStragglerError(
            command,
            leader_returncode,
            stdout=stdout,
            stderr=stderr,
        )

    stdout, stderr = _read_streams(
        stdout_handle,
        stderr_handle,
        text=text,
        encoding=encoding,
        errors=errors,
        max_output_bytes=max_output_bytes,
    )
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed


def _stream_path(configured: str | os.PathLike[str] | None, fallback: Path) -> Path:
    path = fallback if configured is None else Path(configured)
    return RuntimeDirectory.bind(path.parent).ensure(create=True) / path.name


def _open_stream(stack: ExitStack, path: Path) -> IO[bytes]:
    descriptor = open_regular_file(path, truncate=True)
    return stack.enter_context(os.fdopen(descriptor, "w+b"))


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


def _read_interruption_streams(
    stdout: IO[bytes] | None,
    stderr: IO[bytes] | None,
    *,
    text: bool,
    encoding: str | None,
    errors: str | None,
    max_output_bytes: int,
) -> tuple[str | bytes | None, str | bytes | None]:
    """Collect signal evidence without allowing output errors to mask exit."""
    try:
        return _read_streams(
            stdout,
            stderr,
            text=text,
            encoding=encoding,
            errors=errors,
            max_output_bytes=max_output_bytes,
        )
    except (OSError, UnicodeError, ProcessOutputLimitError):
        # The retained log files remain the source of evidence.  TERM/HUP must
        # still escape as SystemExit even if evidence decoding/limits fail.
        return None, None


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
        _signal_posix_group(process, group_id, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            # Reap a cooperative direct child promptly.  On macOS an unreaped
            # zombie keeps killpg(..., 0) true and a later SIGKILL can report
            # EPERM even though no live process remains in the group.
            process.poll()
            if not _posix_group_exists(group_id):
                break
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        process.poll()
        if _posix_group_exists(group_id):
            _signal_posix_group(process, group_id, signal.SIGKILL)
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


def _signal_posix_group(
    process: subprocess.Popen, group_id: int, sig: signal.Signals
) -> None:
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # BSD can return EPERM for a group containing only zombies.  Ignore it
        # only after reaping our leader and proving no live member remains;
        # inability to inspect the process table fails closed.
        process.poll()
        if process.returncode is None or _posix_group_has_live_members(group_id):
            raise


def _posix_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # It exists, but the caller cannot signal it.
        return True
    return True


def _posix_group_has_live_members(group_id: int) -> bool:
    """Return true on a live member or when liveness cannot be proven safely."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pgid=,stat="],
            capture_output=True,
            text=True,
            check=False,
            timeout=1,
            env=credential_reduced_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    for line in result.stdout.splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        try:
            member_group = int(fields[0])
        except ValueError:
            continue
        if member_group == group_id and not fields[1].lstrip().startswith("Z"):
            return True
    return False

"""Ordered, evidence-producing verification gates.

The engine is intentionally independent of the Execute phase.  It consumes
the already-resolved gate configuration, supervises every command, persists
bounded logs, and returns JSON-safe evidence suitable for a Task Run
checkpoint.  Callers supply the Workspace snapshot capability so mutation
policy stays aligned with the controller's Git-visible change boundary.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from machinist.config import GateMutationPolicy, VerificationGateConfig
from machinist.process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ProcessCancelledError,
    ProcessOutputLimitError,
    ProcessStartError,
    ProcessStragglerError,
    ProcessTimeoutError,
    run_supervised,
)
from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    reserve_regular_file,
)

Runner = Callable[..., subprocess.CompletedProcess]
Snapshotter = Callable[[Path], str]
CancelCheck = Callable[[], bool]

DEFAULT_EVIDENCE_CHARACTERS = 4_000


class GateStatus(str, Enum):
    """Stable outcome vocabulary for one verification gate."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    START_ERROR = "start_error"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    STRAGGLER = "straggler"
    MUTATION_DETECTED = "mutation_detected"
    SNAPSHOT_ERROR = "snapshot_error"
    SKIPPED = "skipped"


_ALWAYS_BLOCKING_STATUSES = frozenset(
    {
        GateStatus.CANCELLED,
        GateStatus.MUTATION_DETECTED,
        GateStatus.SNAPSHOT_ERROR,
        GateStatus.STRAGGLER,
    }
)


@dataclass(frozen=True)
class GateResult:
    """One gate outcome with bounded output and durable log evidence."""

    name: str
    command: str
    required: bool
    mutation_policy: str
    status: GateStatus
    duration_seconds: float
    returncode: int | None
    stdout_excerpt: str
    stderr_excerpt: str
    stdout_log: str
    stderr_log: str
    error: str | None = None
    snapshot_before: str | None = None
    snapshot_after: str | None = None

    @property
    def passed(self) -> bool:
        return self.status is GateStatus.PASSED

    @property
    def blocking(self) -> bool:
        # ``required`` controls whether an ordinary command outcome is advisory.
        # Controller invariants are never advisory: cancellation must stop the
        # run, and a forbidden-mutation policy must either be proven or fail
        # closed before the workspace can be committed.
        return (self.required and not self.passed) or (
            self.status in _ALWAYS_BLOCKING_STATUSES
        )

    def as_dict(self) -> dict[str, Any]:
        """Return evidence containing only JSON-compatible values."""

        return {
            "name": self.name,
            "command": self.command,
            "required": self.required,
            "mutation_policy": self.mutation_policy,
            "status": self.status.value,
            "passed": self.passed,
            "blocking": self.blocking,
            "duration_seconds": self.duration_seconds,
            "returncode": self.returncode,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "stdout_log": self.stdout_log,
            "stderr_log": self.stderr_log,
            "error": self.error,
            "snapshot_before": self.snapshot_before,
            "snapshot_after": self.snapshot_after,
        }


@dataclass(frozen=True)
class VerificationReport:
    """Aggregate ordered gate evidence."""

    gates: tuple[GateResult, ...]
    duration_seconds: float

    @property
    def failures(self) -> tuple[GateResult, ...]:
        return tuple(gate for gate in self.gates if gate.blocking)

    @property
    def required_failures(self) -> tuple[GateResult, ...]:
        return tuple(gate for gate in self.gates if gate.required and not gate.passed)

    @property
    def advisory_failures(self) -> tuple[GateResult, ...]:
        return tuple(
            gate for gate in self.gates if not gate.required and not gate.passed
        )

    @property
    def success(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        """Return checkpoint-ready, JSON-safe aggregate evidence."""

        return {
            "success": self.success,
            "duration_seconds": self.duration_seconds,
            "blocking_failures": [gate.name for gate in self.failures],
            "required_failures": [gate.name for gate in self.required_failures],
            "advisory_failures": [gate.name for gate in self.advisory_failures],
            "gates": [gate.as_dict() for gate in self.gates],
        }


class VerificationError(Exception):
    """Base class for stable verification-engine failures."""


class VerificationConfigurationError(VerificationError):
    """The engine cannot enforce the requested gate policy."""


class VerificationFailed(VerificationError):
    """Verification was blocked after all runnable gates completed."""

    def __init__(self, report: VerificationReport):
        self.report = report
        self.failures = report.failures
        summary = ", ".join(
            f"{gate.name} ({gate.status.value})" for gate in self.failures
        )
        details = []
        for gate in self.failures:
            evidence = gate.stderr_excerpt or gate.stdout_excerpt or gate.error
            if evidence:
                details.append(f"{gate.name}: {_tail(evidence.strip(), 1_000)}")
        message = f"verification gates blocked: {summary}"
        if details:
            message += "\n" + "\n".join(details)
        super().__init__(message)


def run_verification_gates(
    cwd: str | Path,
    gates: Sequence[VerificationGateConfig],
    *,
    log_dir: str | Path,
    snapshotter: Snapshotter | None = None,
    runner: Runner = run_supervised,
    cancel_check: CancelCheck | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    evidence_characters: int = DEFAULT_EVIDENCE_CHARACTERS,
) -> VerificationReport:
    """Run ordered gates and raise once if any gate has a blocking outcome.

    Ordinary advisory command failures are retained in the returned report but
    do not raise. Required failures do not short-circuit later gates, allowing
    one run to produce a useful aggregate. Cancellation, forbidden mutation,
    inability to enforce a forbidden-mutation snapshot, and background process
    stragglers are always blocking.
    """

    resolved_cwd = Path(cwd).expanduser().resolve()
    ordered_gates = tuple(gates)
    # Execute historically injects subprocess.run.  Preserve that seam while
    # upgrading the production default to process-group supervision.
    selected_runner = run_supervised if runner is subprocess.run else runner
    if evidence_characters <= 0:
        raise ValueError("evidence_characters must be positive")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")
    if snapshotter is None and any(
        gate.mutation_policy is GateMutationPolicy.FORBID for gate in ordered_gates
    ):
        raise VerificationConfigurationError(
            "mutation-forbidden verification gates require a snapshotter"
        )

    raw_log_dir = Path(log_dir).expanduser()
    try:
        resolved_log_dir = RuntimeDirectory.bind(raw_log_dir).ensure(create=True)
    except (OSError, RuntimePathError) as exc:
        raise VerificationConfigurationError(
            f"could not create verification log directory {raw_log_dir}: {exc}"
        ) from exc
    started_at = time.monotonic()
    results: list[GateResult] = []
    cancelled = False

    for index, gate in enumerate(ordered_gates, start=1):
        stdout_log, stderr_log = _gate_log_paths(resolved_log_dir, index, gate.name)
        try:
            reserve_regular_file(stdout_log)
            reserve_regular_file(stderr_log)
        except (OSError, RuntimePathError) as exc:
            raise VerificationConfigurationError(
                f"could not prepare verification logs in {resolved_log_dir}: {exc}"
            ) from exc
        if cancelled:
            results.append(
                _skipped_result(
                    gate, stdout_log, stderr_log, "not run after cancellation"
                )
            )
            continue

        result = _run_gate(
            resolved_cwd,
            gate,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            snapshotter=snapshotter,
            runner=selected_runner,
            cancel_check=cancel_check,
            max_output_bytes=max_output_bytes,
            evidence_characters=evidence_characters,
        )
        results.append(result)
        cancelled = result.status is GateStatus.CANCELLED

    report = VerificationReport(
        gates=tuple(results),
        duration_seconds=_duration(started_at),
    )
    if not report.success:
        raise VerificationFailed(report)
    return report


def _run_gate(
    cwd: Path,
    gate: VerificationGateConfig,
    *,
    stdout_log: Path,
    stderr_log: Path,
    snapshotter: Snapshotter | None,
    runner: Runner,
    cancel_check: CancelCheck | None,
    max_output_bytes: int,
    evidence_characters: int,
) -> GateResult:
    started_at = time.monotonic()
    snapshot_before: str | None = None
    if gate.mutation_policy is GateMutationPolicy.FORBID:
        assert snapshotter is not None  # checked once before dispatch
        try:
            snapshot_before = snapshotter(cwd)
        except Exception as exc:  # noqa: BLE001 - capability failures become evidence
            return _result(
                gate,
                GateStatus.SNAPSHOT_ERROR,
                started_at,
                stdout_log,
                stderr_log,
                evidence_characters=evidence_characters,
                error=f"could not capture pre-gate snapshot: {exc}",
            )

    status = GateStatus.PASSED
    returncode: int | None = None
    stdout = ""
    stderr = ""
    error: str | None = None
    try:
        completed = runner(
            gate.command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=gate.timeout_minutes * 60,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            max_output_bytes=max_output_bytes,
            cancel_check=cancel_check,
        )
        returncode = completed.returncode
        stdout = _as_text(completed.stdout)
        stderr = _as_text(completed.stderr)
        if completed.returncode != 0:
            status = GateStatus.FAILED
            error = f"command exited with status {completed.returncode}"
    except ProcessTimeoutError as exc:
        status = GateStatus.TIMED_OUT
        stdout = _as_text(exc.output)
        stderr = _as_text(exc.stderr)
        error = f"command timed out after {gate.timeout_minutes} minutes"
    except subprocess.TimeoutExpired as exc:
        # Preserve compatibility with injected subprocess.run-shaped runners.
        status = GateStatus.TIMED_OUT
        stdout = _as_text(exc.output)
        stderr = _as_text(exc.stderr)
        error = f"command timed out after {gate.timeout_minutes} minutes"
    except ProcessStartError as exc:
        status = GateStatus.START_ERROR
        error = str(exc)
    except ProcessCancelledError as exc:
        status = GateStatus.CANCELLED
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        error = str(exc)
    except ProcessOutputLimitError as exc:
        status = GateStatus.OUTPUT_LIMIT
        error = str(exc)
    except ProcessStragglerError as exc:
        status = GateStatus.STRAGGLER
        returncode = exc.leader_returncode
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        error = str(exc)

    snapshot_after: str | None = None
    if gate.mutation_policy is GateMutationPolicy.FORBID:
        assert snapshotter is not None
        try:
            snapshot_after = snapshotter(cwd)
        except Exception as exc:  # noqa: BLE001 - capability failures become evidence
            status = GateStatus.SNAPSHOT_ERROR
            error = f"could not capture post-gate snapshot: {exc}"
        else:
            if snapshot_before != snapshot_after:
                status = GateStatus.MUTATION_DETECTED
                error = "gate changed the mutation-forbidden workspace"

    return _result(
        gate,
        status,
        started_at,
        stdout_log,
        stderr_log,
        evidence_characters=evidence_characters,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error=error,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
    )


def _result(
    gate: VerificationGateConfig,
    status: GateStatus,
    started_at: float,
    stdout_log: Path,
    stderr_log: Path,
    *,
    evidence_characters: int,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
    error: str | None = None,
    snapshot_before: str | None = None,
    snapshot_after: str | None = None,
) -> GateResult:
    stdout_evidence = stdout or _read_log_tail(stdout_log, evidence_characters)
    stderr_evidence = stderr or _read_log_tail(stderr_log, evidence_characters)
    return GateResult(
        name=gate.name,
        command=gate.command,
        required=gate.required,
        mutation_policy=gate.mutation_policy.value,
        status=status,
        duration_seconds=_duration(started_at),
        returncode=returncode,
        stdout_excerpt=_tail(stdout_evidence, evidence_characters),
        stderr_excerpt=_tail(stderr_evidence, evidence_characters),
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        error=error,
        snapshot_before=snapshot_before,
        snapshot_after=snapshot_after,
    )


def _skipped_result(
    gate: VerificationGateConfig,
    stdout_log: Path,
    stderr_log: Path,
    error: str,
) -> GateResult:
    return GateResult(
        name=gate.name,
        command=gate.command,
        required=gate.required,
        mutation_policy=gate.mutation_policy.value,
        status=GateStatus.SKIPPED,
        duration_seconds=0.0,
        returncode=None,
        stdout_excerpt="",
        stderr_excerpt="",
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        error=error,
    )


def _gate_log_paths(log_dir: Path, index: int, name: str) -> tuple[Path, Path]:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "gate"
    prefix = f"{index:02d}-{slug}"
    return log_dir / f"{prefix}.stdout.log", log_dir / f"{prefix}.stderr.log"


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _tail(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[-limit:]


def _read_log_tail(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit * 4))
            return stream.read().decode(errors="replace")[-limit:]
    except OSError:
        return ""


def _duration(started_at: float) -> float:
    return round(max(0.0, time.monotonic() - started_at), 6)

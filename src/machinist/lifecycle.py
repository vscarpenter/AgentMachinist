"""Durable local task-run records and single-process task claims.

The records make failures and recovery checkpoints visible across restarts.  The
file lock prevents two local workers from operating on the same issue at once;
GitHub remains the source of truth for approval and cross-host coordination.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Callable, TypeVar


class LifecycleError(Exception):
    """A task cannot enter the requested lifecycle transition."""


class Phase(str, Enum):
    SPEC = "spec"
    EXECUTE = "execute"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYABLE = "retryable"


@dataclass(frozen=True)
class RunRecord:
    issue: int
    phase: Phase
    status: RunStatus
    attempt: int
    started_at: str
    updated_at: str
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


_T = TypeVar("_T")
_HELD_ISSUES: set[int] = set()
_HELD_LOCK = Lock()


class TaskClaim:
    """The narrow capability given to a running phase."""

    def __init__(self, lifecycle: "TaskLifecycle", record: RunRecord, previous: dict[str, Any]):
        self._lifecycle = lifecycle
        self._record = record
        self.previous_evidence = dict(previous)

    def checkpoint(self, **evidence: Any) -> None:
        """Atomically persist evidence needed to reconcile a partial run."""
        merged = {**self._record.evidence, **evidence}
        self._record = self._lifecycle._replace(self._record, evidence=merged)
        self._lifecycle._write(self._record)


class TaskLifecycle:
    """Own Task Run persistence, retry transitions, and local claims."""

    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)

    def run(
        self,
        issue: int,
        phase: Phase,
        action: Callable[[TaskClaim], _T],
        *,
        repeat_succeeded: bool = False,
    ) -> _T:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with _HELD_LOCK:
            if issue in _HELD_ISSUES:
                raise LifecycleError(f"issue #{issue} is already claimed by this process")
            _HELD_ISSUES.add(issue)

        lock_path = self.runs_dir / f"issue-{issue}.lock"
        lock_file = lock_path.open("a+")
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LifecycleError(f"issue #{issue} is already claimed by another worker") from exc

            prior = self.record(issue, phase)
            repeat = (
                prior is not None
                and prior.status is RunStatus.SUCCEEDED
                and repeat_succeeded
            )
            if prior is not None and prior.status is not RunStatus.RETRYABLE and not repeat:
                if prior.status is RunStatus.FAILED:
                    raise LifecycleError(
                        f"issue #{issue} {phase.value} previously failed; run 'machinist retry {issue}'"
                    )
                raise LifecycleError(
                    f"issue #{issue} {phase.value} is {prior.status.value}; refusing a duplicate run"
                )

            now = _now()
            attempt = 1 if prior is None else prior.attempt + 1
            previous = {} if prior is None else prior.evidence
            running = RunRecord(
                issue=issue,
                phase=phase,
                status=RunStatus.RUNNING,
                attempt=attempt,
                started_at=now,
                updated_at=now,
                evidence=dict(previous),
            )
            self._write(running)
            claim = TaskClaim(self, running, previous)
            try:
                result = action(claim)
            except Exception as exc:
                current = self.record(issue, phase) or running
                self._write(self._replace(current, status=RunStatus.FAILED, error=str(exc)))
                raise

            evidence = dict((self.record(issue, phase) or running).evidence)
            for attr, key in (("number", "pr_number"), ("url", "pr_url")):
                value = getattr(result, attr, None)
                if value is not None:
                    evidence[key] = value
            current = self.record(issue, phase) or running
            self._write(
                self._replace(
                    current,
                    status=RunStatus.SUCCEEDED,
                    error=None,
                    evidence=evidence,
                )
            )
            return result
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
                with _HELD_LOCK:
                    _HELD_ISSUES.discard(issue)

    def retry(self, issue: int, phase: Phase | None = None) -> RunRecord:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        with _HELD_LOCK:
            if issue in _HELD_ISSUES:
                raise LifecycleError(f"issue #{issue} is still claimed by this process")
        lock_file = (self.runs_dir / f"issue-{issue}.lock").open("a+")
        try:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LifecycleError(
                    f"issue #{issue} is still claimed by another worker; stop it before retrying"
                ) from exc
            record = self.record(issue, phase) if phase is not None else self.latest(issue)
            if record is None:
                raise LifecycleError(f"no Task Run exists for issue #{issue}")
            if record.status not in {RunStatus.FAILED, RunStatus.RUNNING}:
                raise LifecycleError(
                    f"issue #{issue} {record.phase.value} is {record.status.value}, not failed or abandoned"
                )
            retryable = self._replace(record, status=RunStatus.RETRYABLE, error=None)
            self._write(retryable)
            return retryable
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def record(self, issue: int, phase: Phase | None = None) -> RunRecord | None:
        if phase is None:
            return self.latest(issue)
        path = self._path(issue, phase)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return RunRecord(
            issue=data["issue"],
            phase=Phase(data["phase"]),
            status=RunStatus(data["status"]),
            attempt=data["attempt"],
            started_at=data["started_at"],
            updated_at=data["updated_at"],
            error=data.get("error"),
            evidence=data.get("evidence", {}),
        )

    def latest(self, issue: int) -> RunRecord | None:
        records = [
            record
            for phase in Phase
            if (record := self.record(issue, phase)) is not None
        ]
        return max(records, key=lambda item: item.updated_at, default=None)

    def _replace(self, record: RunRecord, **changes: Any) -> RunRecord:
        data = asdict(record)
        data.update(changes)
        data["updated_at"] = _now()
        data["phase"] = Phase(data["phase"])
        data["status"] = RunStatus(data["status"])
        return RunRecord(**data)

    def _path(self, issue: int, phase: Phase) -> Path:
        return self.runs_dir / f"issue-{issue}-{phase.value}.json"

    def _write(self, record: RunRecord) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        payload["phase"] = record.phase.value
        payload["status"] = record.status.value
        fd, temporary = tempfile.mkstemp(prefix=".run-", dir=self.runs_dir, text=True)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(record.issue, record.phase))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _now() -> str:
    return datetime.now(UTC).isoformat()

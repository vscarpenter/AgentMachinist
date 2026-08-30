"""Cooperative, durable cancellation requests for local Task Runs."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    open_regular_file,
    read_text_file,
    regular_file_exists,
    unlink_regular_file,
)

_MAX_MARKER_BYTES = 64 * 1024


class CancellationError(Exception):
    """A cancellation marker could not be read or written safely."""


class TaskCancelledError(Exception):
    """A supervised Task stopped after an explicit operator request."""

    cancelled = True


@dataclass(frozen=True)
class CancellationRequest:
    issue: int
    reason: str
    requested_at: str
    requester_pid: int


class CancellationStore:
    def __init__(
        self,
        runs_dir: Path,
        *,
        repo_root: str | Path | None = None,
    ):
        try:
            self._runtime = RuntimeDirectory.bind(runs_dir, repo_root=repo_root)
            self.root = self._runtime.subdirectory("cancellations", create=False)
        except RuntimePathError as exc:
            raise CancellationError(f"unsafe cancellation state path: {exc}") from exc

    def request(self, issue: int, reason: str) -> CancellationRequest:
        _validate_issue(issue)
        reason = reason.strip()
        if not reason or len(reason) > 2_000:
            raise CancellationError("cancellation reason must be 1-2,000 characters")
        request = CancellationRequest(
            issue=issue,
            reason=reason,
            requested_at=datetime.now(UTC).isoformat(),
            requester_pid=os.getpid(),
        )
        self._ensure_root(create=True)
        target = self._path(issue)
        with self._issue_lock(issue):
            try:
                atomic_write_text_file(
                    target,
                    json.dumps(asdict(request), sort_keys=True) + "\n",
                )
            except (OSError, RuntimePathError) as exc:
                raise CancellationError(
                    f"could not request cancellation: {exc}"
                ) from exc
        return request

    def get(self, issue: int) -> CancellationRequest | None:
        _validate_issue(issue)
        self._ensure_root(create=False)
        path = self._path(issue)
        try:
            if not regular_file_exists(path):
                return None
            with self._issue_lock(issue):
                if not regular_file_exists(path):
                    return None
                payload = json.loads(read_text_file(path, max_bytes=_MAX_MARKER_BYTES))
                request = _request_from_payload(payload, expected_issue=issue)
        except (
            OSError,
            RuntimePathError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise CancellationError(
                f"cancellation marker {path} is corrupt; refusing to ignore it: {exc}"
            ) from exc
        return request

    def requested(self, issue: int) -> bool:
        return self.get(issue) is not None

    def clear(self, issue: int) -> bool:
        """Unconditionally clear a marker after an explicit operator command."""
        _validate_issue(issue)
        self._ensure_root(create=False)
        if not regular_file_exists(self._path(issue)):
            return False
        with self._issue_lock(issue):
            try:
                return unlink_regular_file(self._path(issue), missing_ok=True)
            except (OSError, RuntimePathError) as exc:
                raise CancellationError(f"could not clear cancellation: {exc}") from exc

    def clear_if_matches(
        self,
        issue: int,
        expected: CancellationRequest | None,
    ) -> bool:
        """Clear only the generation observed before a lifecycle transition.

        A cancellation requested while retry is changing lifecycle state must
        remain durable. The per-issue file Claim makes compare-and-delete one
        operation relative to concurrent request writers.
        """
        _validate_issue(issue)
        self._ensure_root(create=False)
        path = self._path(issue)
        if not regular_file_exists(path):
            return False
        with self._issue_lock(issue):
            current = self._read_unlocked(issue)
            if current is None or current != expected:
                return False
            try:
                return unlink_regular_file(path, missing_ok=True)
            except (OSError, RuntimePathError) as exc:
                raise CancellationError(f"could not clear cancellation: {exc}") from exc

    def check(self, issue: int):
        """Return a zero-argument callback accepted by supervised processes."""
        return lambda: self.requested(issue)

    def _path(self, issue: int) -> Path:
        return self.root / f"issue-{issue}.json"

    def _read_unlocked(self, issue: int) -> CancellationRequest | None:
        path = self._path(issue)
        try:
            if not regular_file_exists(path):
                return None
            payload = json.loads(read_text_file(path, max_bytes=_MAX_MARKER_BYTES))
            return _request_from_payload(payload, expected_issue=issue)
        except (
            OSError,
            RuntimePathError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise CancellationError(
                f"cancellation marker {path} is corrupt; refusing to ignore it: {exc}"
            ) from exc

    @contextmanager
    def _issue_lock(self, issue: int):
        lock_path = self.root / f"issue-{issue}.lock"
        try:
            descriptor = open_regular_file(lock_path, truncate=False, mode=0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise CancellationError(
                    f"cancellation Claim {lock_path} is not a regular file"
                )
            lock_file = os.fdopen(descriptor, "a+")
            with lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except CancellationError:
            raise
        except (OSError, RuntimePathError) as exc:
            raise CancellationError(
                f"could not hold cancellation Claim: {exc}"
            ) from exc

    def _ensure_root(self, *, create: bool) -> Path:
        try:
            return self._runtime.subdirectory("cancellations", create=create)
        except RuntimePathError as exc:
            raise CancellationError(f"unsafe cancellation state path: {exc}") from exc


def _validate_issue(issue: int) -> None:
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise CancellationError("issue must be a positive integer")


def _request_from_payload(
    payload: object,
    *,
    expected_issue: int,
) -> CancellationRequest:
    if not isinstance(payload, dict):
        raise ValueError("marker must be a JSON object")
    required = {"issue", "reason", "requested_at", "requester_pid"}
    if set(payload) != required:
        raise ValueError("marker fields do not match the cancellation schema")

    issue = payload["issue"]
    reason = payload["reason"]
    requested_at = payload["requested_at"]
    requester_pid = payload["requester_pid"]
    if isinstance(issue, bool) or not isinstance(issue, int) or issue != expected_issue:
        raise ValueError(f"marker does not match issue #{expected_issue}")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000:
        raise ValueError("reason must be a non-empty string up to 2,000 characters")
    if not isinstance(requested_at, str) or not requested_at.strip():
        raise ValueError("requested_at must be a non-empty string")
    try:
        datetime.fromisoformat(requested_at)
    except ValueError as exc:
        raise ValueError("requested_at must be an ISO-8601 timestamp") from exc
    if (
        isinstance(requester_pid, bool)
        or not isinstance(requester_pid, int)
        or requester_pid < 1
    ):
        raise ValueError("requester_pid must be a positive integer")
    return CancellationRequest(issue, reason, requested_at, requester_pid)

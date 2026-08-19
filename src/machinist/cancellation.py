"""Cooperative, durable cancellation requests for local Task Runs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
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
        try:
            atomic_write_text_file(
                target,
                json.dumps(asdict(request), sort_keys=True) + "\n",
            )
        except (OSError, RuntimePathError) as exc:
            raise CancellationError(f"could not request cancellation: {exc}") from exc
        return request

    def get(self, issue: int) -> CancellationRequest | None:
        _validate_issue(issue)
        self._ensure_root(create=False)
        path = self._path(issue)
        try:
            if not regular_file_exists(path):
                return None
            payload = json.loads(read_text_file(path, max_bytes=_MAX_MARKER_BYTES))
            request = CancellationRequest(**payload)
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
        if request.issue != issue or not request.reason.strip():
            raise CancellationError(
                f"cancellation marker {path} does not match issue #{issue}"
            )
        return request

    def requested(self, issue: int) -> bool:
        return self.get(issue) is not None

    def clear(self, issue: int) -> bool:
        _validate_issue(issue)
        self._ensure_root(create=False)
        try:
            return unlink_regular_file(self._path(issue), missing_ok=True)
        except (OSError, RuntimePathError) as exc:
            raise CancellationError(f"could not clear cancellation: {exc}") from exc

    def check(self, issue: int):
        """Return a zero-argument callback accepted by supervised processes."""
        return lambda: self.requested(issue)

    def _path(self, issue: int) -> Path:
        return self.root / f"issue-{issue}.json"

    def _ensure_root(self, *, create: bool) -> Path:
        try:
            return self._runtime.subdirectory("cancellations", create=create)
        except RuntimePathError as exc:
            raise CancellationError(f"unsafe cancellation state path: {exc}") from exc


def _validate_issue(issue: int) -> None:
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise CancellationError("issue must be a positive integer")

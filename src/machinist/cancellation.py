"""Cooperative, durable cancellation requests for local Task Runs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


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
    def __init__(self, runs_dir: Path):
        self.root = Path(runs_dir) / "cancellations"

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
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(issue)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=self.root, text=True
        )
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(asdict(request), stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            raise CancellationError(f"could not request cancellation: {exc}") from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return request

    def get(self, issue: int) -> CancellationRequest | None:
        _validate_issue(issue)
        path = self._path(issue)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            request = CancellationRequest(**payload)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CancellationError(
                f"cancellation marker {path} is corrupt; refusing to ignore it"
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
        try:
            self._path(issue).unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CancellationError(f"could not clear cancellation: {exc}") from exc
        return True

    def check(self, issue: int):
        """Return a zero-argument callback accepted by supervised processes."""
        return lambda: self.requested(issue)

    def _path(self, issue: int) -> Path:
        return self.root / f"issue-{issue}.json"


def _validate_issue(issue: int) -> None:
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise CancellationError("issue must be a positive integer")

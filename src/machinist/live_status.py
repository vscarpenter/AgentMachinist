"""Changed-only status snapshots for terminals and machine consumers."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class StatusSnapshot:
    observed_at: str
    rows: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at, "pipeline": list(self.rows)}


def iter_status_snapshots(
    loader: Callable[[], Sequence[dict[str, Any]]],
    *,
    interval_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> Iterator[StatusSnapshot]:
    """Yield the initial read and later changed reads until interrupted."""
    if interval_seconds <= 0:
        raise ValueError("status watch interval must be positive")
    now = clock or (lambda: datetime.now(UTC))
    prior: tuple[dict[str, Any], ...] | None = None
    while True:
        rows = tuple(loader())
        if rows != prior:
            prior = rows
            yield StatusSnapshot(now().isoformat(), rows)
        sleep(interval_seconds)

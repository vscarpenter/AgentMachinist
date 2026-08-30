"""Changed-only live status snapshot stream."""

from datetime import UTC, datetime

import pytest

from machinist.live_status import iter_status_snapshots


def test_live_status_emits_initial_and_changed_snapshots_only():
    values = iter(
        ([{"state": "approved"}], [{"state": "approved"}], [{"state": "in review"}])
    )
    sleeps = []

    snapshots = iter_status_snapshots(
        lambda: next(values),
        interval_seconds=2,
        sleep=lambda seconds: sleeps.append(seconds),
        clock=lambda: datetime(2026, 8, 30, 12, tzinfo=UTC),
    )

    first = next(snapshots)
    second = next(snapshots)

    assert first.rows == ({"state": "approved"},)
    assert second.rows == ({"state": "in review"},)
    assert first.observed_at == "2026-08-30T12:00:00+00:00"
    assert sleeps == [2, 2]


@pytest.mark.parametrize("interval", [0, -1])
def test_live_status_requires_positive_interval(interval):
    with pytest.raises(ValueError, match="positive"):
        next(iter_status_snapshots(lambda: [], interval_seconds=interval))

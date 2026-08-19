"""Tests for durable, repository-local queue controls."""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from machinist.queue_control import QueueControl, QueueControlError

NOW = datetime(2026, 8, 19, 14, 30, tzinfo=UTC)
LATER = datetime(2026, 8, 19, 15, 45, tzinfo=UTC)


@dataclass(frozen=True)
class FakeTask:
    issue_number: int


def test_missing_state_allows_dispatch_and_inspects_as_json_safe(tmp_path: Path):
    control = QueueControl(tmp_path / ".machinist/runs")

    decision = control.admission(7)
    inspected = control.inspect()

    assert decision.allowed
    assert bool(decision)
    assert decision.reason is None
    assert inspected == {
        "schema_version": 1,
        "state_path": str(control.state_path),
        "exists": False,
        "corrupt": False,
        "error": None,
        "paused": False,
        "pause": None,
        "deferred": {},
        "updated_at": None,
    }
    json.dumps(inspected, allow_nan=False)


def test_pause_and_resume_are_durable_and_preserve_reason_timestamp(tmp_path: Path):
    runs = tmp_path / ".machinist/runs"
    control = QueueControl(runs)

    paused = control.pause("laptop is on battery", now=NOW)
    reloaded = QueueControl(runs)

    assert paused["pause"] == {
        "reason": "laptop is on battery",
        "timestamp": NOW.isoformat(),
    }
    decision = reloaded.admission(FakeTask(42))
    assert not decision.allowed
    assert not bool(decision)
    assert decision.issue_number == 42
    assert decision.reason == (
        "queue paused at 2026-08-19T14:30:00+00:00: laptop is on battery"
    )

    resumed = reloaded.resume(now=LATER)

    assert resumed["paused"] is False
    assert resumed["pause"] is None
    assert resumed["updated_at"] == LATER.isoformat()
    assert QueueControl(runs).admission(42).allowed


def test_defer_and_allow_only_the_selected_issue(tmp_path: Path):
    control = QueueControl(tmp_path / ".machinist/runs")

    inspected = control.defer(17, "waiting for API credentials", now=NOW)

    assert inspected["deferred"] == {
        "17": {
            "reason": "waiting for API credentials",
            "timestamp": NOW.isoformat(),
        }
    }
    denied = QueueControl(control.runs_dir).admission(FakeTask(17))
    assert not denied.allowed
    assert denied.reason == (
        "issue #17 deferred at 2026-08-19T14:30:00+00:00: waiting for API credentials"
    )
    assert control.admission(18).allowed

    allowed = control.allow(17, now=LATER)

    assert allowed["deferred"] == {}
    assert control.admission(17).allowed


def test_pause_and_issue_deferrals_are_independent(tmp_path: Path):
    control = QueueControl(tmp_path / "runs")
    control.defer(7, "not today", now=NOW)
    control.pause("maintenance", now=NOW)

    control.resume(now=LATER)

    assert not control.admission(7).allowed
    assert control.admission(8).allowed


def test_admission_accepts_issue_number_and_issue_shaped_tasks(tmp_path: Path):
    control = QueueControl(tmp_path / "runs")
    control.defer(9, "blocked", now=NOW)

    @dataclass(frozen=True)
    class LifecycleTask:
        issue: int

    assert not control.admission(9).allowed
    assert not control.admission(FakeTask(9)).allowed
    assert not control.admission(LifecycleTask(9)).allowed
    assert not control.admission({"issue_number": 9}).allowed

    with pytest.raises(ValueError, match="positive issue number"):
        control.admission(0)
    with pytest.raises(ValueError, match="issue_number"):
        control.admission(object())


def test_state_write_is_private_atomic_and_leaves_no_temporary_file(tmp_path: Path):
    control = QueueControl(tmp_path / "runs")

    control.pause("maintenance", now=NOW)

    assert control.state_path.is_file()
    assert stat.S_IMODE(control.state_path.stat().st_mode) == 0o600
    assert list(control.runs_dir.glob(".queue-control.*.tmp")) == []
    persisted = json.loads(control.state_path.read_text())
    assert persisted["schema_version"] == 1
    assert persisted["pause"]["reason"] == "maintenance"


@pytest.mark.parametrize(
    "bad_state",
    [
        "{not-json}\n",
        json.dumps({"schema_version": 999, "pause": None, "deferred": {}}),
        json.dumps(
            {
                "schema_version": 1,
                "pause": None,
                "deferred": {"0": {"reason": "bad", "timestamp": NOW.isoformat()}},
                "updated_at": NOW.isoformat(),
            }
        ),
    ],
)
def test_corrupt_state_fails_closed_and_is_not_overwritten(
    tmp_path: Path, bad_state: str
):
    control = QueueControl(tmp_path / "runs")
    control.runs_dir.mkdir(parents=True)
    control.state_path.write_text(bad_state)

    decision = control.admission(7)
    inspected = control.inspect()

    assert not decision.allowed
    assert decision.reason == "queue control state is corrupt; dispatch denied"
    assert inspected["corrupt"] is True
    assert inspected["paused"] is True
    assert inspected["error"]
    json.dumps(inspected, allow_nan=False)
    with pytest.raises(QueueControlError, match="corrupt"):
        control.resume(now=NOW)
    assert control.state_path.read_text() == bad_state


def test_concurrent_deferrals_do_not_lose_updates(tmp_path: Path):
    runs = tmp_path / "runs"

    def defer(issue_number: int) -> None:
        QueueControl(runs).defer(issue_number, f"reason {issue_number}", now=NOW)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(defer, range(1, 33)))

    inspected = QueueControl(runs).inspect()
    assert list(inspected["deferred"]) == [str(issue) for issue in range(1, 33)]
    assert all(
        not QueueControl(runs).admission(issue).allowed for issue in range(1, 33)
    )


def test_controls_are_isolated_per_runs_directory(tmp_path: Path):
    first = QueueControl(tmp_path / "first/.machinist/runs")
    second = QueueControl(tmp_path / "second/.machinist/runs")
    first.pause("only first", now=NOW)

    assert not first.admission(7).allowed
    assert second.admission(7).allowed


def test_reasons_are_nonempty_and_bounded(tmp_path: Path):
    control = QueueControl(tmp_path / "runs")

    with pytest.raises(ValueError, match="reason"):
        control.defer(7, "   ")
    with pytest.raises(ValueError, match="reason"):
        control.pause("")
    with pytest.raises(ValueError, match="1000"):
        control.pause("x" * 1001)

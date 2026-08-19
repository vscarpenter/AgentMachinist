from datetime import UTC, datetime
from pathlib import Path

from machinist.admission import queue_admission
from machinist.config import MachinistConfig
from machinist.lifecycle import Phase, TaskLifecycle


def test_queue_admission_allows_without_optional_policy(tmp_path: Path):
    decision = queue_admission(
        MachinistConfig(),
        TaskLifecycle(tmp_path / "runs"),
        now=datetime(2026, 8, 19, 12, tzinfo=UTC),
    )

    assert decision.allowed
    assert decision.reason is None


def test_queue_admission_enforces_allowed_hours(tmp_path: Path):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "allowed_hours": {
                    "start": "09:00",
                    "end": "17:00",
                    "timezone": "UTC",
                    "days": ["wed"],
                }
            }
        }
    )

    decision = queue_admission(
        config,
        TaskLifecycle(tmp_path / "runs"),
        now=datetime(2026, 8, 19, 18, tzinfo=UTC),
    )

    assert not decision.allowed
    assert "allowed_hours" in decision.reason


def test_queue_admission_counts_durable_attempts_toward_daily_budget(
    tmp_path: Path,
):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "task_budget": {
                    "max_tasks_per_day": 1,
                    "timezone": "UTC",
                }
            }
        }
    )
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(42, Phase.SPEC, lambda claim: None)
    record = lifecycle.record(42, Phase.SPEC)
    assert record is not None
    # Use the persisted record's actual day so this remains timezone-stable.
    now = datetime.fromisoformat(record.started_at).astimezone(UTC)

    decision = queue_admission(config, lifecycle, now=now)

    assert not decision.allowed
    assert "daily Task budget" in decision.reason


def test_queue_admission_counts_orphan_history_toward_daily_task_budget(
    tmp_path: Path,
):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "task_budget": {
                    "max_tasks_per_day": 1,
                    "timezone": "UTC",
                }
            }
        }
    )
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(42, Phase.SPEC, lambda claim: None)
    projection = tmp_path / "runs" / "issue-42-spec.json"
    record = lifecycle.record(42, Phase.SPEC)
    assert record is not None
    projection.unlink()
    assert lifecycle.inventory().records == ()

    decision = queue_admission(
        config,
        lifecycle,
        now=datetime.fromisoformat(record.started_at).astimezone(UTC),
    )

    assert not decision.allowed
    assert "daily Task budget" in decision.reason


def test_queue_admission_counts_orphan_history_toward_daily_runtime_budget(
    tmp_path: Path,
    monkeypatch,
):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "task_budget": {
                    "max_runtime_minutes_per_day": 1,
                    "timezone": "UTC",
                }
            }
        }
    )
    lifecycle = TaskLifecycle(tmp_path / "runs")
    timestamps = iter(
        [
            "2026-08-19T12:00:00+00:00",
            "2026-08-19T12:02:00+00:00",
        ]
    )
    monkeypatch.setattr("machinist.lifecycle._now", lambda: next(timestamps))
    lifecycle.run(42, Phase.EXECUTE, lambda claim: None)
    (tmp_path / "runs" / "issue-42-execute.json").write_text("{not-json\n")
    assert lifecycle.inventory().corrupt

    decision = queue_admission(
        config,
        lifecycle,
        now=datetime(2026, 8, 19, 12, 3, tzinfo=UTC),
    )

    assert not decision.allowed
    assert "daily runtime budget" in decision.reason


def test_queue_admission_fails_closed_when_corruption_hides_budget_evidence(
    tmp_path: Path,
):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "task_budget": {
                    "max_tasks_per_day": 2,
                    "timezone": "UTC",
                }
            }
        }
    )
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(42, Phase.SPEC, lambda claim: None)
    record = lifecycle.record(42, Phase.SPEC)
    assert record is not None
    (tmp_path / "runs" / "issue-42-spec.json").write_text("{not-json\n")

    decision = queue_admission(
        config,
        lifecycle,
        now=datetime.fromisoformat(record.started_at).astimezone(UTC),
    )

    assert not decision.allowed
    assert "history is corrupt" in decision.reason


def test_queue_admission_does_not_double_count_projection_and_history(
    tmp_path: Path,
):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "task_budget": {
                    "max_tasks_per_day": 2,
                    "timezone": "UTC",
                }
            }
        }
    )
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(42, Phase.SPEC, lambda claim: None)
    record = lifecycle.record(42, Phase.SPEC)
    assert record is not None

    decision = queue_admission(
        config,
        lifecycle,
        now=datetime.fromisoformat(record.started_at).astimezone(UTC),
    )

    assert decision.allowed


def test_queue_admission_accounts_for_same_pass_reservations(tmp_path: Path):
    config = MachinistConfig.model_validate(
        {
            "queue": {
                "task_budget": {
                    "max_tasks_per_day": 1,
                    "timezone": "UTC",
                }
            }
        }
    )

    decision = queue_admission(
        config,
        TaskLifecycle(tmp_path / "runs"),
        now=datetime(2026, 8, 19, 12, tzinfo=UTC),
        additionally_admitted=1,
    )

    assert not decision.allowed

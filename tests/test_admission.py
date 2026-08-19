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

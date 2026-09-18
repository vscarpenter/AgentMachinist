"""Aggregate local reporting and redacted OTLP export."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from machinist.lifecycle import Phase, RunRecord, RunStatus
from machinist.local_tasks import LocalTask
from machinist.reporting import (
    ReportingError,
    build_metrics_report,
    parse_since_duration,
)
from machinist.telemetry import build_otlp_payload, export_otlp


def record(
    *,
    phase: Phase,
    status: RunStatus,
    attempt: int,
    duration: float,
    error: str | None = None,
    evidence: dict | None = None,
) -> RunRecord:
    return RunRecord(
        issue=42,
        phase=phase,
        status=status,
        attempt=attempt,
        started_at="2026-08-29T12:00:00+00:00",
        updated_at="2026-08-29T12:00:10+00:00",
        ended_at="2026-08-29T12:00:10+00:00",
        duration_seconds=duration,
        error=error,
        evidence=evidence or {},
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12h", timedelta(hours=12)),
        ("30d", timedelta(days=30)),
        ("2w", timedelta(weeks=2)),
    ],
)
def test_parse_since_duration_accepts_integer_hours_days_and_weeks(
    value: str, expected: timedelta
) -> None:
    assert parse_since_duration(value) == expected


@pytest.mark.parametrize("value", ["", "0d", "1m", "1.5d", "-2h"])
def test_parse_since_duration_rejects_ambiguous_windows(value: str) -> None:
    with pytest.raises(ReportingError, match="integer.*h, d, or w"):
        parse_since_duration(value)


def test_report_aggregates_outcomes_retries_durations_and_safe_evidence() -> None:
    records = (
        record(
            phase=Phase.EXECUTE,
            status=RunStatus.FAILED,
            attempt=1,
            duration=10,
            error="ExecutePhaseError: secret task content",
            evidence={
                "current_stage": "verification 1/2",
                "verification_report": {
                    "gates": [{"name": "private name", "status": "failed"}]
                },
                "prompt": "do not include me",
            },
        ),
        record(
            phase=Phase.EXECUTE,
            status=RunStatus.SUCCEEDED,
            attempt=2,
            duration=30,
            evidence={
                "harness": {
                    "name": "fixture",
                    "model": "model-a",
                    "structured_usage": True,
                },
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            },
        ),
        record(
            phase=Phase.REVIEW,
            status=RunStatus.CANCELLED,
            attempt=1,
            duration=20,
        ),
    )

    report = build_metrics_report(
        records,
        since=datetime(2026, 8, 1, tzinfo=UTC),
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    payload = report.to_dict()

    assert payload["attempts"] == 3
    assert payload["success_rate"] == pytest.approx(1 / 3)
    assert payload["retry_count"] == 1
    assert payload["cancellation_count"] == 1
    assert payload["duration_seconds"] == {"median": 20.0, "p95": 30.0}
    assert payload["gate_failures"] == {"failed": 1}
    assert payload["failure_categories"] == {"ExecutePhaseError@verification": 1}
    assert payload["token_totals"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    serialized = json.dumps(payload)
    assert "secret task content" not in serialized
    assert "do not include me" not in serialized
    assert "private name" not in serialized


def test_otlp_payload_uses_only_allowlisted_attributes() -> None:
    report = build_metrics_report(
        (
            record(
                phase=Phase.EXECUTE,
                status=RunStatus.SUCCEEDED,
                attempt=1,
                duration=4,
                evidence={"harness": {"name": "codex", "model": "gpt-safe"}},
            ),
        ),
        since=datetime(2026, 8, 1, tzinfo=UTC),
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    payload = build_otlp_payload(report, repository="owner/repo")
    serialized = json.dumps(payload)
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    points = [
        point
        for metric in metrics
        for data in (metric.get("sum") or metric.get("gauge"),)
        for point in data["dataPoints"]
    ]
    attribute_keys = {
        attribute["key"]
        for point in points
        for attribute in point.get("attributes", [])
    }

    assert attribute_keys <= {"repository", "phase", "status", "harness", "model"}
    assert "owner/repo" in serialized
    assert "issue" not in serialized.casefold()


def test_export_sends_json_with_bounded_auth_and_no_response_body(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        def close(self) -> None:
            return None

    def opener(request, *, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("MACHINIST_OTLP_AUTHORIZATION", "Bearer test-secret")
    payload = {"resourceMetrics": []}

    export_otlp(
        "https://telemetry.example.test/v1/metrics",
        payload,
        timeout_seconds=4,
        opener=opener,
    )

    request = captured["request"]
    assert captured["timeout"] == 4
    assert request.get_header("Authorization") == "Bearer test-secret"
    assert json.loads(request.data) == payload


@pytest.mark.parametrize(
    "endpoint",
    [
        "ftp://example.test/metrics",
        "https://user:pass@example.test/metrics",
        "https:///missing",
    ],
)
def test_export_rejects_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(ReportingError, match="OTLP endpoint"):
        export_otlp(endpoint, {"resourceMetrics": []}, timeout_seconds=5)


def test_report_keeps_task_namespaces_distinct_and_deduplicates_attempts() -> None:
    legacy = record(
        phase=Phase.EXECUTE, status=RunStatus.FAILED, attempt=1, duration=10
    )
    local = replace(legacy, status=RunStatus.SUCCEEDED)

    payload = build_metrics_report(
        [legacy, legacy],
        local_records=[local, local],
        since=datetime(2026, 8, 1, tzinfo=UTC),
    ).to_dict()

    assert payload["attempts"] == 2
    assert payload["by_source"] == {"legacy": 1, "local": 1}
    assert payload["task_counts"] == {"legacy": 1, "local": 1}
    assert payload["first_pass_execute"] == {
        "terminal_attempts": 2,
        "succeeded_without_repair": 1,
        "success_rate": 0.5,
    }


def test_usage_coverage_preserves_missing_usage_as_unknown() -> None:
    unknown = record(
        phase=Phase.EXECUTE, status=RunStatus.SUCCEEDED, attempt=1, duration=10
    )
    known = replace(
        unknown,
        issue=43,
        evidence={
            "harness": {"structured_usage": True},
            "usage": {"input_tokens": 0, "output_tokens": -1, "total_tokens": True},
        },
    )

    payload = build_metrics_report(
        [unknown, known], since=datetime(2026, 8, 1, tzinfo=UTC)
    ).to_dict()

    assert payload["token_totals"] == {"input_tokens": 0}
    assert payload["usage_coverage"] == {
        "attempts_with_usage": 1,
        "attempts_without_usage": 1,
        "by_token": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 0},
    }


def test_repaired_success_is_not_first_pass_and_resume_does_not_double_count() -> None:
    repair = {
        "version": 1,
        "max_attempts": 1,
        "attempts_consumed": 1,
        "deadline_at": "2026-08-29T12:10:00+00:00",
        "status": "succeeded",
        "initial_verification_report": {"gates": []},
        "attempts": [
            {
                "attempt": 1,
                "started_at": "2026-08-29T12:00:00+00:00",
                "deadline_at": "2026-08-29T12:10:00+00:00",
                "ended_at": "2026-08-29T12:00:04+00:00",
                "status": "succeeded",
                "harness_completed": True,
                "harness_report_excerpt": "private repair output",
                "harness_duration_seconds": 2.0,
                "duration_seconds": 4.0,
                "verification_report": {"gates": []},
            }
        ],
    }
    original = record(
        phase=Phase.EXECUTE,
        status=RunStatus.SUCCEEDED,
        attempt=1,
        duration=10,
        evidence={"repair": repair},
    )
    resumed = replace(original, attempt=2, updated_at="2026-08-29T12:01:00+00:00")

    payload = build_metrics_report(
        [original, resumed],
        local_records=[original],
        since=datetime(2026, 8, 1, tzinfo=UTC),
    ).to_dict()

    assert payload["success_rate"] == 1
    assert payload["first_pass_execute"] == {
        "terminal_attempts": 2,
        "succeeded_without_repair": 0,
        "success_rate": 0.0,
    }
    assert payload["repairs"] == {
        "attempted_rounds": 2,
        "verified_rounds": 2,
        "unsuccessful_rounds": 0,
        "incomplete_rounds": 0,
        "success_rate": 1.0,
        "duration_seconds": {"total": 8.0, "median": 4.0, "p95": 4.0},
    }
    assert "private repair output" not in json.dumps(payload)


def delivery_task() -> LocalTask:
    spec, candidate = "a" * 40, "b" * 40
    return LocalTask(
        number=42,
        title="private objective",
        body="private task body",
        repository="/private/repository",
        base_branch="main",
        base_sha="c" * 40,
        branch="machinist/task-42",
        created_at="2026-07-01T00:00:00+00:00",
        updated_at="2026-08-29T12:00:10+00:00",
        revision=3,
        spec_sha=spec,
        candidate_sha=candidate,
        approval={
            "repository": "/private/repository",
            "task_id": "T42",
            "spec_sha": spec,
        },
        review_report={"completed": True, "reviewed_sha": candidate},
        integration={"candidate_sha": candidate, "observed_sha": candidate},
        publication={"stage": "published", "published_sha": candidate},
    )


def delivery_records() -> tuple[RunRecord, ...]:
    return (
        record(
            phase=Phase.EXECUTE,
            status=RunStatus.SUCCEEDED,
            attempt=1,
            duration=10,
            evidence={"implementation_sha": "b" * 40, "approved_sha": "a" * 40},
        ),
        record(
            phase=Phase.REVIEW,
            status=RunStatus.SUCCEEDED,
            attempt=1,
            duration=10,
            evidence={"reviewed_sha": "b" * 40},
        ),
    )


def test_local_delivery_counts_exact_current_snapshots_not_legacy_aliases() -> None:
    task = delivery_task()
    records = delivery_records()
    stale_task = replace(task, number=43, candidate_sha="d" * 40)

    payload = build_metrics_report(
        records,
        local_records=records,
        local_tasks=[task, stale_task],
        since=datetime(2026, 8, 1, tzinfo=UTC),
    ).to_dict()

    assert payload["local_delivery"] == {
        "tasks_updated": 2,
        "reviewed_candidates": 1,
        "integrated": 1,
        "published": 1,
    }
    text = json.dumps(payload)
    assert "private" not in text
    assert "T42" not in text
    legacy_only = build_metrics_report(
        records, local_tasks=[task], since=datetime(2026, 8, 1, tzinfo=UTC)
    )
    assert legacy_only.local_delivery["reviewed_candidates"] == 0


def test_delivery_snapshot_uses_evidence_before_window_but_filters_task_updates() -> (
    None
):
    task = replace(delivery_task(), updated_at="2026-09-01T00:00:00+00:00")

    payload = build_metrics_report(
        [],
        local_records=delivery_records(),
        local_tasks=[task, replace(task, updated_at="2026-07-01T00:00:00+00:00")],
        since=datetime(2026, 9, 1, tzinfo=UTC),
    ).to_dict()

    assert payload["attempts"] == 0
    assert payload["local_delivery"]["tasks_updated"] == 1
    assert payload["local_delivery"]["reviewed_candidates"] == 1


@pytest.mark.parametrize("phase", [Phase.EXECUTE, Phase.REVIEW])
def test_delivery_rejects_newer_failed_phase_attempt(phase: Phase) -> None:
    records = delivery_records()
    failed = replace(
        next(record for record in records if record.phase is phase),
        attempt=2,
        status=RunStatus.FAILED,
    )

    payload = build_metrics_report(
        [],
        local_records=[*records, failed],
        local_tasks=[delivery_task()],
        since=datetime(2026, 8, 1, tzinfo=UTC),
    ).to_dict()

    assert payload["local_delivery"]["reviewed_candidates"] == 0

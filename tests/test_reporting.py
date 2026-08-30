"""Aggregate local reporting and redacted OTLP export."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from machinist.lifecycle import Phase, RunRecord, RunStatus
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

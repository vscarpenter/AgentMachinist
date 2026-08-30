"""Allowlisted OTLP/HTTP JSON projection for aggregate Task Run metrics."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from machinist.reporting import MetricsReport, ReportingError

_AUTHORIZATION_ENV = "MACHINIST_OTLP_AUTHORIZATION"
_MAX_AUTHORIZATION_CHARS = 4_096


def build_otlp_payload(
    report: MetricsReport,
    *,
    repository: str,
) -> dict[str, Any]:
    """Return OTLP/HTTP JSON containing only allowlisted aggregate attributes."""
    timestamp = str(int(datetime.fromisoformat(report.generated_at).timestamp() * 1e9))
    metrics = [
        _sum_metric(
            "machinist.attempts",
            [
                _point(
                    item.count,
                    timestamp,
                    repository=repository,
                    phase=item.phase,
                    status=item.status,
                    harness=item.harness,
                    model=item.model,
                )
                for item in report.series
            ],
        ),
        _sum_metric(
            "machinist.retries",
            [_point(report.retry_count, timestamp, repository=repository)],
        ),
        _sum_metric(
            "machinist.cancellations",
            [_point(report.cancellation_count, timestamp, repository=repository)],
        ),
    ]
    _append_gauges(metrics, report, repository=repository, timestamp=timestamp)
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": []},
                "scopeMetrics": [
                    {"scope": {"name": "agentmachinist"}, "metrics": metrics}
                ],
            }
        ]
    }


def validate_otlp_endpoint(endpoint: str) -> str:
    """Validate one explicit HTTP(S) destination without embedded credentials."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ReportingError(
            "OTLP endpoint must be an HTTP(S) URL without credentials or fragments"
        )
    return endpoint


def export_otlp(
    endpoint: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: int,
    opener: Callable[..., Any] = urlopen,
) -> None:
    """Send one bounded OTLP JSON request and never expose response content."""
    target = validate_otlp_endpoint(endpoint)
    request = Request(
        target,
        data=json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    _add_authorization(request)
    response = None
    try:
        response = opener(request, timeout=timeout_seconds)
        status = getattr(response, "status", 200)
        if not isinstance(status, int) or status < 200 or status >= 300:
            raise ReportingError(f"OTLP export failed with HTTP status {status}")
    except ReportingError:
        raise
    except HTTPError as exc:
        raise ReportingError(f"OTLP export failed with HTTP status {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ReportingError(f"OTLP export failed: {type(exc).__name__}") from exc
    finally:
        if response is not None:
            response.close()


def _add_authorization(request: Request) -> None:
    authorization = os.environ.get(_AUTHORIZATION_ENV)
    if not authorization:
        return
    if (
        len(authorization) > _MAX_AUTHORIZATION_CHARS
        or "\n" in authorization
        or "\r" in authorization
    ):
        raise ReportingError(f"{_AUTHORIZATION_ENV} is invalid")
    request.add_unredirected_header("Authorization", authorization)


def _point(
    value: int | float,
    timestamp: str,
    **attributes: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timeUnixNano": timestamp,
        "attributes": [
            {"key": key, "value": {"stringValue": item}}
            for key, item in attributes.items()
            if item is not None
        ],
    }
    payload["asInt" if type(value) is int else "asDouble"] = (
        str(value) if type(value) is int else value
    )
    return payload


def _sum_metric(name: str, points: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "sum": {
            "dataPoints": points,
            "aggregationTemporality": 2,
            "isMonotonic": False,
        },
    }


def _gauge_metric(name: str, point: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "gauge": {"dataPoints": [point]}}


def _append_gauges(
    metrics: list[dict[str, Any]],
    report: MetricsReport,
    *,
    repository: str,
    timestamp: str,
) -> None:
    values = {
        "machinist.success_rate": report.success_rate,
        "machinist.duration.median": report.duration_seconds["median"],
        "machinist.duration.p95": report.duration_seconds["p95"],
    }
    for name, value in values.items():
        if value is not None:
            metrics.append(
                _gauge_metric(
                    name,
                    _point(value, timestamp, repository=repository),
                )
            )

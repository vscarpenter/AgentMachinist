"""JSON-safe local Task Run observability."""

import json

import pytest

from machinist.lifecycle import Phase, TaskLifecycle
from machinist.observability import build_run_report, summarize_run_report


def _raise(error):
    raise error


def test_report_separates_current_history_and_orphaned_attempts(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(RuntimeError, match="first attempt"):
        lifecycle.run(
            7,
            Phase.EXECUTE,
            lambda claim: _raise(RuntimeError("first attempt")),
        )
    lifecycle.retry(7, Phase.EXECUTE)
    lifecycle.run(7, Phase.EXECUTE, lambda claim: None)

    report = build_run_report(lifecycle, issue=7)
    payload = report.to_dict()

    assert [(item["attempt"], item["status"]) for item in payload["current"]] == [
        (2, "succeeded")
    ]
    assert [(item["attempt"], item["status"]) for item in payload["history"]] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert [(item["attempt"], item["status"]) for item in payload["orphans"]] == [
        (1, "failed")
    ]
    assert payload["corrupt"] == []
    assert payload["sources"] == {}
    json.dumps(payload, allow_nan=False)


def test_report_includes_history_only_and_projection_only_records(tmp_path):
    runs_dir = tmp_path / "runs"
    lifecycle = TaskLifecycle(runs_dir)
    lifecycle.run(11, Phase.SPEC, lambda claim: None)
    (runs_dir / "issue-11-spec.json").unlink()

    projection_only = runs_dir / "issue-12-spec.json"
    projection_only.write_text(
        '{"attempt":1,"error":null,"evidence":{"legacy":true},'
        '"issue":12,"phase":"spec",'
        '"started_at":"2026-08-19T00:00:00+00:00",'
        '"status":"succeeded",'
        '"updated_at":"2026-08-19T00:00:01+00:00"}\n'
    )

    report = build_run_report(lifecycle)
    payload = report.to_dict()

    assert [(item["issue"], item["attempt"]) for item in payload["current"]] == [
        (12, 1)
    ]
    assert [(item["issue"], item["attempt"]) for item in payload["history"]] == [
        (11, 1),
        (12, 1),
    ]
    assert [(item["issue"], item["attempt"]) for item in payload["orphans"]] == [
        (11, 1)
    ]


def test_report_preserves_corrupt_projection_and_journal_paths(tmp_path):
    runs_dir = tmp_path / "runs"
    lifecycle = TaskLifecycle(runs_dir)
    lifecycle.run(21, Phase.SPEC, lambda claim: None)
    journal = runs_dir / "history" / "issue-21-spec" / "attempt-000001.jsonl"
    with journal.open("a") as stream:
        stream.write("{not-json}\n")
    projection = runs_dir / "issue-22-execute.json"
    projection.write_text("{not-json}\n")

    payload = build_run_report(lifecycle).to_dict()

    assert payload["corrupt"] == [
        {
            "path": str(journal),
            "kind": "journal",
            "issue": 21,
            "phase": "spec",
            "attempt": 1,
        },
        {
            "path": str(projection),
            "kind": "projection",
            "issue": 22,
            "phase": "execute",
            "attempt": None,
        },
    ]
    assert payload["history"][0]["status"] == "succeeded"
    json.dumps(payload, allow_nan=False)


def test_remote_sources_are_independent_json_safe_envelopes(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(31, Phase.SPEC, lambda claim: None)

    report = build_run_report(
        lifecycle,
        issue=31,
        remote_sources={
            "github": lambda: _raise(RuntimeError("GitHub unavailable")),
            "workspace": lambda: {"exists": True, "path": "/tmp/worktree"},
            "invalid": lambda: {"opaque": object()},
        },
    )
    payload = report.to_dict()

    assert payload["current"][0]["status"] == "succeeded"
    assert payload["sources"]["workspace"] == {
        "ok": True,
        "data": {"exists": True, "path": "/tmp/worktree"},
        "error": None,
    }
    assert payload["sources"]["github"] == {
        "ok": False,
        "data": None,
        "error": {
            "source": "github",
            "type": "RuntimeError",
            "message": "GitHub unavailable",
        },
    }
    assert payload["sources"]["invalid"]["ok"] is False
    assert payload["sources"]["invalid"]["error"]["type"] == "TypeError"
    assert [error["source"] for error in payload["source_errors"]] == [
        "github",
        "invalid",
    ]
    json.dumps(payload, allow_nan=False)


def test_issue_scope_filters_records_and_corrupt_artifacts(tmp_path):
    runs_dir = tmp_path / "runs"
    lifecycle = TaskLifecycle(runs_dir)
    lifecycle.run(41, Phase.SPEC, lambda claim: None)
    lifecycle.run(42, Phase.SPEC, lambda claim: None)
    corrupt_41 = runs_dir / "issue-41-execute.json"
    corrupt_41.write_text("bad")
    (runs_dir / "issue-42-execute.json").write_text("bad")

    payload = build_run_report(lifecycle, issue=41).to_dict()

    assert {record["issue"] for record in payload["current"]} == {41}
    assert {record["issue"] for record in payload["history"]} == {41}
    assert [artifact["path"] for artifact in payload["corrupt"]] == [str(corrupt_41)]


def test_human_summary_surfaces_run_and_partial_failure_counts(tmp_path):
    runs_dir = tmp_path / "runs"
    lifecycle = TaskLifecycle(runs_dir)
    with pytest.raises(RuntimeError):
        lifecycle.run(51, Phase.EXECUTE, lambda claim: _raise(RuntimeError("boom")))
    (runs_dir / "issue-51-spec.json").write_text("bad")

    report = build_run_report(
        lifecycle,
        issue=51,
        remote_sources={"github": lambda: _raise(RuntimeError("offline"))},
    )
    lines = summarize_run_report(report)

    assert lines[0] == "Issue #51: 1 current projection, 1 recorded attempt."
    assert "execute failed (attempt 1" in lines[1]
    assert "1 corrupt Task Run artifact" in lines
    assert "github unavailable: RuntimeError: offline" in lines


def test_human_summary_distinguishes_interrupted_projection_from_live_claim(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    now = "2026-08-30T12:00:00+00:00"
    lifecycle.runs_dir.mkdir()
    (lifecycle.runs_dir / "issue-61-execute.json").write_text(
        '{"attempt":1,"error":null,"evidence":{"current_stage":"verification 1/2"},'
        f'"issue":61,"phase":"execute","started_at":"{now}",'
        f'"status":"running","updated_at":"{now}"}}\n'
    )

    lines = summarize_run_report(build_run_report(lifecycle), lifecycle=lifecycle)

    assert any("#61 execute interrupted" in line for line in lines)
    assert any("stage: verification 1/2" in line for line in lines)
    assert "    Next: machinist retry 61 --phase execute" in lines


def test_successful_spec_summary_points_to_the_human_approval_gate(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(62, Phase.SPEC, lambda claim: None)

    lines = summarize_run_report(build_run_report(lifecycle), lifecycle=lifecycle)

    assert "    Next: machinist approve --issue 62" in lines


def test_report_discovers_review_history(tmp_path) -> None:
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(63, Phase.REVIEW, lambda claim: None)

    payload = build_run_report(lifecycle).to_dict()

    assert [(item["phase"], item["status"]) for item in payload["history"]] == [
        ("review", "succeeded")
    ]


@pytest.mark.parametrize("issue", [0, -1, True])
def test_issue_scope_must_be_a_positive_integer(tmp_path, issue):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(ValueError, match="positive integer"):
        build_run_report(lifecycle, issue=issue)

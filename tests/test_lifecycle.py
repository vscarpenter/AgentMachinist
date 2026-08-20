"""Durable Task Run and Claim behavior."""

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from machinist.lifecycle import LifecycleError, Phase, RunStatus, TaskLifecycle


def test_run_persists_success_and_result_evidence(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    result = lifecycle.run(
        42,
        Phase.SPEC,
        lambda claim: SimpleNamespace(number=57, url="https://github.com/x/y/pull/57"),
    )

    assert result.number == 57
    record = lifecycle.record(42, Phase.SPEC)
    assert record.status is RunStatus.SUCCEEDED
    assert record.attempt == 1
    assert record.evidence["pr_number"] == 57
    assert record.ended_at is not None
    assert record.duration_seconds is not None
    assert record.duration_seconds >= 0

    projection = json.loads((tmp_path / "runs" / "issue-42-spec.json").read_text())
    assert projection["schema_version"] == 1
    assert projection["status"] == "succeeded"
    assert projection["evidence"]["pr_number"] == 57


def test_failure_is_durable_and_requires_explicit_retry(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(RuntimeError, match="boom"):
        lifecycle.run(
            42, Phase.EXECUTE, lambda claim: (_ for _ in ()).throw(RuntimeError("boom"))
        )

    record = lifecycle.record(42, Phase.EXECUTE)
    assert record.status is RunStatus.FAILED
    assert record.error == "boom"

    with pytest.raises(LifecycleError, match="machinist retry 42"):
        lifecycle.run(42, Phase.EXECUTE, lambda claim: None)


def test_retry_preserves_checkpoint_evidence_for_reconciliation(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    def fail_after_push(claim):
        claim.checkpoint(approved_sha="a" * 40, implementation_sha="b" * 40)
        raise RuntimeError("ready transition failed")

    with pytest.raises(RuntimeError):
        lifecycle.run(42, Phase.EXECUTE, fail_after_push)

    lifecycle.retry(42, Phase.EXECUTE)
    seen = {}

    def resume(claim):
        seen.update(claim.previous_evidence)

    lifecycle.run(42, Phase.EXECUTE, resume)

    assert seen["implementation_sha"] == "b" * 40
    assert lifecycle.record(42, Phase.EXECUTE).attempt == 2


def test_attempt_history_is_append_only_across_retry(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(RuntimeError, match="first attempt"):
        lifecycle.run(
            42,
            Phase.EXECUTE,
            lambda claim: (_ for _ in ()).throw(RuntimeError("first attempt")),
        )

    lifecycle.retry(42, Phase.EXECUTE)
    first_journal = (
        tmp_path / "runs" / "history" / "issue-42-execute" / "attempt-000001.jsonl"
    )
    first_attempt_before_retry_run = first_journal.read_bytes()

    lifecycle.run(
        42, Phase.EXECUTE, lambda claim: SimpleNamespace(number=58, url="https://x/58")
    )

    assert first_journal.read_bytes() == first_attempt_before_retry_run
    journals = sorted(first_journal.parent.glob("attempt-*.jsonl"))
    assert [path.name for path in journals] == [
        "attempt-000001.jsonl",
        "attempt-000002.jsonl",
    ]
    for path in journals:
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert events
        assert all(event["schema_version"] == 1 for event in events)

    history = lifecycle.history(42, Phase.EXECUTE)
    assert [(record.attempt, record.status) for record in history] == [
        (1, RunStatus.FAILED),
        (2, RunStatus.SUCCEEDED),
    ]


def test_checkpoint_rejects_non_json_evidence_without_persisting_it(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    def invalid_checkpoint(claim):
        claim.checkpoint(opaque=object())

    with pytest.raises(LifecycleError, match="evidence"):
        lifecycle.run(42, Phase.EXECUTE, invalid_checkpoint)

    record = lifecycle.record(42, Phase.EXECUTE)
    assert record.status is RunStatus.FAILED
    assert "opaque" not in record.evidence


def test_nested_claim_for_same_issue_is_refused(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    def nested(_claim):
        lifecycle.run(42, Phase.EXECUTE, lambda claim: None)

    with pytest.raises(LifecycleError, match="already claimed"):
        lifecycle.run(42, Phase.SPEC, nested)


def test_latest_failed_phase_can_be_queried(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    with pytest.raises(RuntimeError):
        lifecycle.run(
            7, Phase.SPEC, lambda claim: (_ for _ in ()).throw(RuntimeError("bad spec"))
        )

    record = lifecycle.latest(7)

    assert record.phase is Phase.SPEC
    assert record.status is RunStatus.FAILED


def test_explicit_retry_can_recover_an_abandoned_running_record(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    now = "2026-08-17T00:00:00+00:00"
    path = tmp_path / "runs" / "issue-9-execute.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"attempt":1,"error":null,"evidence":{"approved_sha":"abc"},'
        f'"issue":9,"phase":"execute","started_at":"{now}",'
        f'"status":"running","updated_at":"{now}"}}\n'
    )

    record = lifecycle.retry(9, Phase.EXECUTE)

    assert record.status is RunStatus.RETRYABLE
    assert record.evidence["approved_sha"] == "abc"
    assert lifecycle.history(9, Phase.EXECUTE)[0].status is RunStatus.ABANDONED


def test_explicit_repeat_can_start_a_second_attempt_after_success(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(4, Phase.EXECUTE, lambda claim: None)

    lifecycle.run(4, Phase.EXECUTE, lambda claim: None, repeat_succeeded=True)

    assert lifecycle.record(4, Phase.EXECUTE).attempt == 2


def test_keyboard_interrupt_is_durable_cancelled_attempt_and_re_raised(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(KeyboardInterrupt):
        lifecycle.run(
            5,
            Phase.EXECUTE,
            lambda claim: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    record = lifecycle.record(5, Phase.EXECUTE)
    assert record.status is RunStatus.CANCELLED
    assert record.ended_at is not None
    assert record.duration_seconds is not None
    assert lifecycle.history(5, Phase.EXECUTE)[0].status is RunStatus.CANCELLED


def test_explicit_cancelled_exception_is_persisted_as_cancelled(tmp_path):
    class ExplicitCancellation(Exception):
        cancelled = True

    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(ExplicitCancellation):
        lifecycle.run(
            42,
            Phase.EXECUTE,
            lambda claim: (_ for _ in ()).throw(ExplicitCancellation("stopped")),
        )

    record = lifecycle.record(42, Phase.EXECUTE)
    assert record is not None
    assert record.status is RunStatus.CANCELLED
    assert not lifecycle.claim_held(5)


def test_non_exception_base_exception_is_abandoned_and_re_raised(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(SystemExit):
        lifecycle.run(
            6,
            Phase.SPEC,
            lambda claim: (_ for _ in ()).throw(SystemExit("shutdown")),
        )

    record = lifecycle.record(6, Phase.SPEC)
    assert record.status is RunStatus.ABANDONED
    assert record.error == "shutdown"


def test_cancelled_system_exit_is_durable_cancelled_and_re_raised(tmp_path):
    class ServiceTermination(SystemExit):
        cancelled = True

    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(ServiceTermination):
        lifecycle.run(
            16,
            Phase.EXECUTE,
            lambda claim: (_ for _ in ()).throw(ServiceTermination(143)),
        )

    record = lifecycle.record(16, Phase.EXECUTE)
    assert record.status is RunStatus.CANCELLED
    assert record.ended_at is not None


@pytest.mark.skipif(os.name != "posix", reason="signal assertions require POSIX")
def test_sigterm_during_nonprocess_action_is_durable_and_exits_without_traceback(
    tmp_path,
):
    runs_dir = tmp_path / "runs"
    action_started = tmp_path / "action-started"
    worker_code = (
        "import time; from pathlib import Path; "
        "from machinist.lifecycle import Phase, TaskLifecycle; "
        f"runs = Path({str(runs_dir)!r}); "
        f"started = Path({str(action_started)!r}); "
        "lifecycle = TaskLifecycle(runs); "
        "lifecycle.run(42, Phase.EXECUTE, "
        "lambda _claim: (started.write_text('started'), time.sleep(60)))"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_code],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        projection = runs_dir / "issue-42-execute.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
            projection.exists() and action_started.exists()
        ):
            if worker.poll() is not None:
                break
            time.sleep(0.02)
        assert projection.exists() and action_started.exists()
        assert json.loads(projection.read_text())["status"] == "running"

        worker.send_signal(signal.SIGTERM)
        stdout, stderr = worker.communicate(timeout=5)

        assert worker.returncode == 128 + signal.SIGTERM
        assert stdout == ""
        assert "Traceback" not in stderr
        payload = json.loads(projection.read_text())
        assert payload["status"] == "cancelled"
        assert payload["ended_at"] is not None
        assert payload["error"] == "Task Run interrupted by SIGTERM"
        events = [
            json.loads(line)
            for line in (
                runs_dir / "history" / "issue-42-execute" / "attempt-000001.jsonl"
            )
            .read_text()
            .splitlines()
        ]
        assert events[-1]["event"] == "cancelled"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=3)


def test_completed_spec_can_be_explicitly_abandoned_then_revised(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    def finish_while_proving_active_claim_is_protected(_claim):
        with pytest.raises(LifecycleError, match="already claimed"):
            lifecycle.abandon(12, Phase.SPEC, "superseded while still running")

    lifecycle.run(12, Phase.SPEC, finish_while_proving_active_claim_is_protected)
    succeeded = lifecycle.record(12, Phase.SPEC)

    abandoned = lifecycle.abandon(12, Phase.SPEC, "issue intent changed")

    assert abandoned.status is RunStatus.ABANDONED
    assert abandoned.error == "issue intent changed"
    assert abandoned.ended_at == succeeded.ended_at
    assert abandoned.duration_seconds == succeeded.duration_seconds
    assert lifecycle.history(12, Phase.SPEC) == [abandoned]
    with pytest.raises(LifecycleError, match="previously abandoned"):
        lifecycle.run(12, Phase.SPEC, lambda claim: None)

    lifecycle.run(
        12,
        Phase.SPEC,
        lambda claim: None,
        repeat_succeeded=True,
    )
    assert lifecycle.record(12, Phase.SPEC).attempt == 2


def test_claim_exposes_identity_and_contained_log_paths(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    observed = {}

    def capture_claim(claim):
        observed.update(
            issue=claim.issue,
            phase=claim.phase,
            attempt=claim.attempt,
            log_path=claim.log_path("verification.log"),
        )
        for unsafe in ("", ".", "..", "../escape", "nested/file", "bad\\name"):
            with pytest.raises(LifecycleError, match="log name"):
                claim.log_path(unsafe)

    lifecycle.run(13, Phase.EXECUTE, capture_claim)

    expected = (
        tmp_path
        / "runs"
        / "logs"
        / "issue-13"
        / "execute"
        / "attempt-1"
        / "verification.log"
    ).resolve()
    assert observed == {
        "issue": 13,
        "phase": Phase.EXECUTE,
        "attempt": 1,
        "log_path": expected,
    }
    assert expected.parent.is_dir()
    assert expected.is_file()
    assert not expected.is_symlink()


def test_claim_held_is_nonblocking_for_local_and_external_claims(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    observed = []

    assert not lifecycle.claim_held(7)
    lifecycle.run(7, Phase.SPEC, lambda claim: observed.append(lifecycle.claim_held(7)))
    assert observed == [True]
    assert not lifecycle.claim_held(7)

    lock_path = tmp_path / "runs" / "issue-8.lock"
    lock_path.touch()
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert lifecycle.claim_held(8)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    assert not lifecycle.claim_held(8)


def test_older_and_corrupt_projection_records_are_handled_safely(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    older = runs_dir / "issue-9-spec.json"
    older.write_text(
        '{"attempt":1,"error":null,"evidence":{"spec_sha":"abc"},'
        '"issue":9,"phase":"spec","started_at":"2026-08-17T00:00:00+00:00",'
        '"status":"succeeded","updated_at":"2026-08-17T00:00:01+00:00"}\n'
    )
    corrupt = runs_dir / "issue-10-execute.json"
    corrupt.write_text("{not json}\n")

    record = lifecycle.record(9, Phase.SPEC)
    assert record.status is RunStatus.SUCCEEDED
    assert record.ended_at is None
    assert record.duration_seconds is None
    assert lifecycle.history(9, Phase.SPEC) == [record]

    with pytest.raises(LifecycleError, match="corrupt"):
        lifecycle.record(10, Phase.EXECUTE)

    inventory = lifecycle.inventory()
    assert inventory.records == (record,)
    assert inventory.corrupt == (corrupt,)


def test_oversized_projection_is_rejected_before_payload_read(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.runs_dir.mkdir()
    projection = lifecycle.runs_dir / "issue-42-spec.json"
    projection.touch()
    with projection.open("r+b") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)

    with pytest.raises(LifecycleError, match="too large"):
        lifecycle.record(42, Phase.SPEC)

    assert lifecycle.inventory().corrupt == (projection,)


def test_oversized_journal_is_reported_corrupt_without_payload_read(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    journal = (
        lifecycle.runs_dir / "history" / "issue-42-execute" / "attempt-000001.jsonl"
    )
    journal.parent.mkdir(parents=True)
    journal.touch()
    with journal.open("r+b") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)

    assert lifecycle.history(42, Phase.EXECUTE) == []
    assert lifecycle.inventory().corrupt == (journal,)


@pytest.mark.parametrize(
    ("relative", "reported"),
    [
        (
            "issue-42-execute/attempt-invalid.jsonl",
            "issue-42-execute/attempt-invalid.jsonl",
        ),
        (
            "issue-invalid-execute/attempt-000001.jsonl",
            "issue-invalid-execute",
        ),
    ],
)
def test_inventory_reports_noncanonical_history_artifacts(tmp_path, relative, reported):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    artifact = lifecycle.runs_dir / "history" / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n")

    assert lifecycle.inventory().corrupt == (lifecycle.runs_dir / "history" / reported,)

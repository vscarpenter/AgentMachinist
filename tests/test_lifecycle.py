"""Durable Task Run and Claim behavior."""

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


def test_failure_is_durable_and_requires_explicit_retry(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    with pytest.raises(RuntimeError, match="boom"):
        lifecycle.run(42, Phase.EXECUTE, lambda claim: (_ for _ in ()).throw(RuntimeError("boom")))

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
        return None

    lifecycle.run(42, Phase.EXECUTE, resume)

    assert seen["implementation_sha"] == "b" * 40
    assert lifecycle.record(42, Phase.EXECUTE).attempt == 2


def test_nested_claim_for_same_issue_is_refused(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")

    def nested(_claim):
        lifecycle.run(42, Phase.EXECUTE, lambda claim: None)

    with pytest.raises(LifecycleError, match="already claimed"):
        lifecycle.run(42, Phase.SPEC, nested)


def test_latest_failed_phase_can_be_queried(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    with pytest.raises(RuntimeError):
        lifecycle.run(7, Phase.SPEC, lambda claim: (_ for _ in ()).throw(RuntimeError("bad spec")))

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


def test_explicit_repeat_can_start_a_second_attempt_after_success(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(4, Phase.EXECUTE, lambda claim: None)

    lifecycle.run(4, Phase.EXECUTE, lambda claim: None, repeat_succeeded=True)

    assert lifecycle.record(4, Phase.EXECUTE).attempt == 2

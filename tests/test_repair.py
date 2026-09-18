"""Bounded repair consumes durable budget before invoking paid work."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from machinist.config import MachinistConfig, VerificationRepairConfig
from machinist.evidence import EvidenceError, TaskEvidence, checkpoint_evidence
from machinist.repair import (
    RepairCancelled,
    RepairDeadlineExceeded,
    RepairInterrupted,
    verify_with_repair,
)
from machinist.verification import (
    GateResult,
    GateStatus,
    VerificationFailed,
    VerificationReport,
)


def _report(status=GateStatus.PASSED, *, code=None, required=True):
    return VerificationReport(
        gates=(
            GateResult(
                name="tests",
                command="run-tests",
                required=required,
                mutation_policy="forbid",
                status=status,
                duration_seconds=0.5,
                returncode=(0 if status is GateStatus.PASSED else 1)
                if code is None
                else code,
                stdout_excerpt="assert expected == actual",
                stderr_excerpt="TOKEN=secret-value\n\x1b[31massertion failed",
                stdout_log="tests.stdout.log",
                stderr_log="tests.stderr.log",
            ),
        ),
        duration_seconds=0.5,
    )


class Scenario:
    def __init__(self):
        self.clock = datetime(2026, 9, 18, tzinfo=UTC)
        self.saved = {}
        self.checkpoints = []
        self.rounds = []
        self.prompts = []
        self.reports = [_report(GateStatus.FAILED), _report()]

    def checkpoint(self, values):
        self.saved = checkpoint_evidence("execute", self.saved, values)
        self.checkpoints.append(deepcopy(self.saved))

    def verify(self, round_number, cancel_check):
        self.rounds.append(round_number)
        report = self.reports.pop(0)
        if not report.success:
            raise VerificationFailed(report)
        return report

    def implement(self, prompt, cancel_check):
        assert self.saved["repair"]["attempts_consumed"] == 1
        assert self.saved["repair"]["status"] == "running"
        assert self.saved["repair"]["attempts"][0]["harness_completed"] is False
        self.prompts.append(prompt)
        self.clock += timedelta(seconds=12)
        return "Repaired the assertion. token=private-report"

    def run(self, **overrides):
        args = dict(
            policy=VerificationRepairConfig(max_attempts=1, timeout_minutes=1),
            evidence=TaskEvidence.load(self.saved),
            checkpoint=self.checkpoint,
            verify=self.verify,
            implement=self.implement,
            approved_prompt="Implement only the approved Spec. Do not commit.",
            change_summary={"files_changed": 1},
            now=lambda: self.clock,
        )
        args.update(overrides)
        return verify_with_repair(**args)


def test_default_is_disabled_and_does_not_change_old_evidence():
    config = MachinistConfig()
    assert config.verification.repair.max_attempts == 0
    assert config.verification.repair.timeout_minutes == 10
    scenario = Scenario()
    with pytest.raises(VerificationFailed):
        scenario.run(policy=config.verification.repair)
    assert scenario.rounds == [0]
    assert scenario.prompts == []
    assert scenario.checkpoints == []


@pytest.mark.parametrize("attempts", [-1, 2, True, "1"])
def test_repair_attempt_limit_accepts_only_integer_zero_or_one(attempts):
    with pytest.raises(ValidationError):
        VerificationRepairConfig(max_attempts=attempts)


@pytest.mark.parametrize("minutes", [0, 241, True, "10"])
def test_repair_time_limit_is_a_strict_bounded_integer(minutes):
    with pytest.raises(ValidationError):
        VerificationRepairConfig(timeout_minutes=minutes)


def test_successful_repair_preserves_rounds_and_authoritative_final_report():
    scenario = Scenario()
    result = scenario.run()
    assert result.success
    assert scenario.rounds == [0, 1]
    repair = scenario.saved["repair"]
    assert repair["attempts_consumed"] == 1
    assert repair["initial_verification_report"]["success"] is False
    assert repair["status"] == "succeeded"
    attempt = repair["attempts"][0]
    assert attempt["harness_completed"] is True
    assert attempt["harness_duration_seconds"] == 12
    assert attempt["duration_seconds"] == 12
    assert attempt["verification_report"] == scenario.saved["verification_report"]
    assert "private-report" not in attempt["harness_report_excerpt"]
    assert scenario.saved["verification_report"]["success"] is True
    prompt = scenario.prompts[0]
    assert "Implement only the approved Spec" in prompt
    assert "untrusted" in prompt
    assert "files_changed" in prompt
    assert "secret-value" not in prompt
    assert "\x1b" not in prompt
    assert "[REDACTED]" in prompt


def test_first_pass_success_and_advisory_failure_do_not_consume_repair():
    for report in (_report(), _report(GateStatus.FAILED, required=False)):
        scenario = Scenario()
        scenario.reports = [report]
        assert scenario.run().success
        assert scenario.prompts == []
        assert "repair" not in scenario.saved


@pytest.mark.parametrize(
    "status",
    [
        status
        for status in GateStatus
        if status not in {GateStatus.FAILED, GateStatus.PASSED}
    ],
)
def test_nonordinary_gate_status_does_not_dispatch_repair(status):
    scenario = Scenario()
    scenario.reports = [_report(status)]
    with pytest.raises(VerificationFailed):
        scenario.run()
    assert scenario.prompts == []
    assert "repair" not in scenario.saved


@pytest.mark.parametrize("code", [-9, 126, 127, 130, 255])
def test_unavailable_commands_and_signal_exits_do_not_dispatch_repair(code):
    scenario = Scenario()
    scenario.reports = [_report(GateStatus.FAILED, code=code)]
    with pytest.raises(VerificationFailed):
        scenario.run()
    assert scenario.prompts == []


def test_advisory_infrastructure_failure_suppresses_otherwise_eligible_repair():
    scenario = Scenario()
    failed = _report(GateStatus.FAILED)
    timed_out = _report(GateStatus.TIMED_OUT, required=False)
    scenario.reports = [VerificationReport(failed.gates + timed_out.gates, 1)]
    with pytest.raises(VerificationFailed):
        scenario.run()
    assert scenario.prompts == []


def test_failed_final_verification_never_starts_another_repair():
    scenario = Scenario()
    scenario.reports = [_report(GateStatus.FAILED), _report(GateStatus.FAILED)]
    with pytest.raises(VerificationFailed):
        scenario.run()
    assert len(scenario.prompts) == 1
    assert scenario.saved["repair"]["status"] == "failed"
    assert scenario.saved["verification_report"]["success"] is False
    with pytest.raises(RepairInterrupted, match="fresh"):
        scenario.run()
    assert len(scenario.prompts) == 1


def test_crash_after_budget_checkpoint_cannot_replay_paid_work_on_resume():
    scenario = Scenario()

    def crash(prompt, cancel_check):
        assert scenario.saved["repair"]["attempts_consumed"] == 1
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        scenario.run(implement=crash)
    assert scenario.saved["repair"]["status"] == "running"
    with pytest.raises(RepairInterrupted, match="fresh"):
        scenario.run()
    assert scenario.saved["repair"]["status"] == "interrupted"
    assert scenario.prompts == []
    assert scenario.rounds == [0]


def test_resume_finished_harness_only_repeats_unfinished_verification():
    scenario = Scenario()

    def crash_verification(round_number, cancel_check):
        if round_number == 1:
            raise KeyboardInterrupt
        return scenario.verify(round_number, cancel_check)

    with pytest.raises(KeyboardInterrupt):
        scenario.run(verify=crash_verification)
    assert scenario.saved["repair"]["status"] == "verifying"
    deadline = scenario.saved["repair"]["deadline_at"]
    scenario.clock += timedelta(seconds=10)
    assert scenario.run().success
    assert len(scenario.prompts) == 1
    assert scenario.saved["repair"]["deadline_at"] == deadline
    assert scenario.saved["repair"]["attempts"][0]["duration_seconds"] == 22


def test_expired_resume_does_not_start_verification_or_replenish_budget():
    scenario = Scenario()
    scenario.run()
    old_rounds = list(scenario.rounds)
    deadline = scenario.saved["repair"]["deadline_at"]
    scenario.clock += timedelta(seconds=60)
    with pytest.raises(RepairDeadlineExceeded):
        scenario.run(
            policy=VerificationRepairConfig(max_attempts=0, timeout_minutes=240)
        )
    assert scenario.rounds == old_rounds
    assert scenario.saved["repair"]["deadline_at"] == deadline
    assert scenario.saved["repair"]["status"] == "timed_out"


@pytest.mark.parametrize("where", ["harness", "verification"])
def test_deadline_covers_harness_and_verification_and_discards_late_success(where):
    scenario = Scenario()
    original_implement = scenario.implement
    original_verify = scenario.verify

    def implement(prompt, check):
        value = original_implement(prompt, check)
        if where == "harness":
            scenario.clock += timedelta(seconds=60)
            assert check() is True
        return value

    def verify(round_number, check):
        value = original_verify(round_number, check)
        if round_number == 1 and where == "verification":
            scenario.clock += timedelta(seconds=60)
            assert check() is True
        return value

    with pytest.raises(RepairDeadlineExceeded):
        scenario.run(implement=implement, verify=verify)
    assert scenario.saved["repair"]["status"] == "timed_out"
    assert scenario.rounds == ([0] if where == "harness" else [0, 1])


def test_user_cancellation_is_distinct_from_deadline_and_not_replayable():
    scenario = Scenario()
    cancelled = False

    def implement(prompt, check):
        nonlocal cancelled
        cancelled = True
        assert check() is True
        raise RuntimeError("subprocess cancelled")

    with pytest.raises(RepairCancelled):
        scenario.run(implement=implement, cancel_check=lambda: cancelled)
    assert scenario.saved["repair"]["status"] == "cancelled"
    assert scenario.saved["repair"]["attempts"][0]["harness_completed"] is False


def test_callback_control_failure_is_preserved_and_budget_is_consumed():
    scenario = Scenario()

    def custody_failure(prompt, check):
        raise ValueError("custody mismatch")

    with pytest.raises(ValueError, match="custody mismatch"):
        scenario.run(implement=custody_failure)
    assert scenario.saved["repair"]["status"] == "failed"
    assert scenario.saved["repair"]["attempts"][0]["harness_completed"] is False
    with pytest.raises(RepairInterrupted):
        scenario.run()


def test_budget_checkpoint_failure_prevents_harness_invocation():
    scenario = Scenario()

    def cannot_save(values):
        if "repair" in values:
            raise OSError("disk full")
        scenario.checkpoint(values)

    with pytest.raises(OSError, match="disk full"):
        scenario.run(checkpoint=cannot_save)
    assert scenario.prompts == []


def test_repair_evidence_is_execute_owned_and_malformed_resume_fails_closed():
    scenario = Scenario()
    scenario.run()
    assert TaskEvidence.load(scenario.saved).repair == scenario.saved["repair"]
    with pytest.raises(EvidenceError, match="repair.*Execute"):
        checkpoint_evidence("spec", {}, {"repair": scenario.saved["repair"]})
    scenario.saved["repair"]["attempts_consumed"] = 0
    with pytest.raises(EvidenceError, match="repair"):
        scenario.run()


@pytest.mark.parametrize("malformed", ["corrupt", [], {"attempts_consumed": 0}])
def test_disabled_policy_still_rejects_malformed_retained_repair(malformed):
    scenario = Scenario()
    scenario.saved["repair"] = malformed
    with pytest.raises(EvidenceError, match="repair"):
        scenario.run(policy=VerificationRepairConfig())
    assert scenario.rounds == []
    assert scenario.prompts == []


def test_clock_rollback_cannot_extend_live_repair_deadline(monkeypatch):
    scenario = Scenario()
    monotonic = [10.0]
    monkeypatch.setattr("machinist.repair.time.monotonic", lambda: monotonic[0])

    def implement(prompt, check):
        scenario.clock -= timedelta(hours=1)
        monotonic[0] += 61
        assert check() is True
        return "finished too late"

    with pytest.raises(RepairDeadlineExceeded):
        scenario.run(implement=implement)
    assert scenario.saved["repair"]["status"] == "timed_out"
    assert scenario.rounds == [0]


def test_existing_completed_repair_never_replenishes_consumed_budget():
    scenario = Scenario()
    scenario.run()
    scenario.reports = [_report()]
    assert scenario.run(policy=VerificationRepairConfig(max_attempts=0)).success
    assert len(scenario.prompts) == 1
    assert scenario.rounds == [0, 1, 1]
    assert scenario.saved["repair"]["attempts_consumed"] == 1


def test_effective_config_exposes_repair_and_harness_gate_permissions():
    config = MachinistConfig.model_validate(
        {
            "verification": {
                "harness_may_run_gates": False,
                "repair": {"max_attempts": 1},
            }
        }
    )
    projected = config.effective_projection()["verification"]
    assert projected["repair"] == {"max_attempts": 1, "timeout_minutes": 10}
    assert projected["harness_may_run_gates"] is False


def test_failure_diagnostic_and_change_summary_are_bounded():
    scenario = Scenario()
    scenario.run(change_summary={"details": "X" * 100_000})
    assert len(scenario.prompts[0]) < 18_000


@pytest.mark.parametrize("mode", ["disabled", "passed", "ineligible", "resumed"])
def test_prompt_supplier_is_not_evaluated_without_new_paid_repair(mode):
    scenario = Scenario()

    def unavailable_prompt():
        raise AssertionError("approved prompt must not be reconstructed")

    if mode == "resumed":
        scenario.run()
        scenario.reports = [_report()]
    elif mode == "passed":
        scenario.reports = [_report()]
    elif mode == "ineligible":
        scenario.reports = [_report(GateStatus.START_ERROR)]
    policy = VerificationRepairConfig(max_attempts=0 if mode == "disabled" else 1)
    if mode in {"disabled", "ineligible"}:
        with pytest.raises(VerificationFailed):
            scenario.run(policy=policy, approved_prompt=unavailable_prompt)
    else:
        assert scenario.run(policy=policy, approved_prompt=unavailable_prompt).success


def test_lazy_prompt_is_evaluated_once_after_durable_budget_consumption():
    scenario = Scenario()
    supplied = []

    def approved_prompt():
        assert scenario.saved["repair"]["attempts_consumed"] == 1
        supplied.append(True)
        return "Exact original approved instructions"

    scenario.run(approved_prompt=approved_prompt)
    assert supplied == [True]
    assert scenario.prompts[0].startswith("Exact original approved instructions")


def test_prompt_construction_that_exhausts_deadline_never_dispatches_harness():
    scenario = Scenario()

    def slow_prompt():
        scenario.clock += timedelta(seconds=61)
        return "Approved instructions"

    with pytest.raises(RepairDeadlineExceeded):
        scenario.run(approved_prompt=slow_prompt)
    assert scenario.prompts == []
    assert scenario.rounds == [0]
    assert scenario.saved["repair"]["status"] == "timed_out"
    assert scenario.saved["repair"]["attempts"][0]["harness_completed"] is False


def test_prompt_supplier_failure_preserves_error_and_never_dispatches_harness():
    scenario = Scenario()

    def missing_instructions():
        raise OSError("approved instruction overlay disappeared")

    with pytest.raises(OSError, match="instruction overlay disappeared"):
        scenario.run(approved_prompt=missing_instructions)
    assert scenario.prompts == []
    assert scenario.rounds == [0]
    assert scenario.saved["repair"]["status"] == "failed"
    with pytest.raises(RepairInterrupted):
        scenario.run()


def test_diagnostic_sanitization_precedes_json_escaping():
    scenario = Scenario()
    failed = _report(GateStatus.FAILED)
    gate = replace(
        failed.gates[0],
        stdout_excerpt="before\x1b]52;c;clipboard-payload\x07after",
    )
    scenario.reports[0] = VerificationReport((gate,), 0.5)
    scenario.run(change_summary={"paths": ["\x1b]52;c;summary-payload\x07changed.py"]})
    assert "clipboard-payload" not in scenario.prompts[0]
    assert "summary-payload" not in scenario.prompts[0]
    assert "changed.py" in scenario.prompts[0]


@pytest.mark.parametrize(
    "alter",
    [
        lambda repair: repair.update(version=2),
        lambda repair: repair.update(attempts=[]),
        lambda repair: repair.update(attempts=[False]),
        lambda repair: repair.update(status="unknown"),
        lambda repair: repair.update(deadline_at="invalid-date"),
        lambda repair: repair.update(deadline_at="2026-09-18T00:01:00"),
        lambda repair: repair.update(initial_verification_report=[]),
        lambda repair: repair["attempts"][0].update(attempt=True),
        lambda repair: repair["attempts"][0].update(harness_completed=False),
        lambda repair: repair["attempts"][0].update(duration_seconds=True),
        lambda repair: repair["attempts"][0].update(harness_duration_seconds=-1),
        lambda repair: repair["attempts"][0].update(verification_report=[]),
        lambda repair: repair["attempts"][0].update(harness_report_excerpt=[]),
        lambda repair: repair["attempts"][0].update(ended_at=False),
    ],
)
def test_malformed_budget_fields_cannot_resume_paid_or_unpaid_work(alter):
    scenario = Scenario()
    scenario.run()
    alter(scenario.saved["repair"])
    rounds = list(scenario.rounds)
    with pytest.raises(EvidenceError, match="repair"):
        scenario.run()
    assert scenario.rounds == rounds
    assert len(scenario.prompts) == 1

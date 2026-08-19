"""Tests for ordered, evidence-producing verification gates."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from machinist.config import (
    GateMutationPolicy,
    MachinistConfig,
    VerificationGateConfig,
)
from machinist.process import (
    ProcessCancelledError,
    ProcessOutputLimitError,
    ProcessStartError,
    ProcessTimeoutError,
)
from machinist.verification import (
    GateStatus,
    VerificationConfigurationError,
    VerificationFailed,
    run_verification_gates,
)


def test_runs_resolved_gates_in_order_with_independent_timeouts_and_aggregation(
    tmp_path,
):
    calls = []
    responses = {
        "required-check": (2, "required output", "required error"),
        "advisory-check": (3, "advisory output", "advisory error"),
        "passing-check": (0, "passed", ""),
    }

    def runner(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        returncode, stdout, stderr = responses[command]
        Path(kwargs["stdout_log"]).write_text(stdout)
        Path(kwargs["stderr_log"]).write_text(stderr)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    gates = (
        _gate("required", "required-check", timeout=1, mutation="allow"),
        _gate("advisory", "advisory-check", timeout=2, required=False),
        _gate("passing", "passing-check", timeout=3, mutation="allow"),
    )

    with pytest.raises(VerificationFailed) as caught:
        run_verification_gates(
            tmp_path,
            gates,
            log_dir=tmp_path / "logs",
            snapshotter=lambda _path: "unchanged",
            runner=runner,
        )

    report = caught.value.report
    assert calls == [
        ("required-check", 60),
        ("advisory-check", 120),
        ("passing-check", 180),
    ]
    assert [result.name for result in report.gates] == [
        "required",
        "advisory",
        "passing",
    ]
    assert [result.status for result in report.gates] == [
        GateStatus.FAILED,
        GateStatus.FAILED,
        GateStatus.PASSED,
    ]
    assert [result.name for result in caught.value.failures] == ["required"]
    assert [result.name for result in report.advisory_failures] == ["advisory"]
    assert "required error" in str(caught.value)


def test_advisory_failure_returns_successful_json_safe_report(tmp_path):
    gate = _gate("lint", "lint", required=False)

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "lint output", "lint warning")

    report = run_verification_gates(
        tmp_path,
        (gate,),
        log_dir=tmp_path / "logs",
        snapshotter=lambda _path: "same",
        runner=runner,
    )

    assert report.success is True
    assert report.gates[0].status is GateStatus.FAILED
    assert report.gates[0].blocking is False
    payload = report.as_dict()
    assert payload["advisory_failures"] == ["lint"]
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


def test_real_supervisor_retains_bounded_output_and_log_evidence(tmp_path):
    code = (
        "import sys; "
        "print('prefix-' + 'x' * 30); "
        "print('prefix-' + 'y' * 30, file=sys.stderr)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    gate = _gate("unit tests", command, mutation="allow")

    report = run_verification_gates(
        tmp_path,
        (gate,),
        log_dir=tmp_path / "logs",
        evidence_characters=12,
        runner=subprocess.run,
    )

    result = report.gates[0]
    assert result.status is GateStatus.PASSED
    assert result.stdout_excerpt == "x" * 11 + "\n"
    assert result.stderr_excerpt == "y" * 11 + "\n"
    assert Path(result.stdout_log).read_text() == "prefix-" + "x" * 30 + "\n"
    assert Path(result.stderr_log).read_text() == "prefix-" + "y" * 30 + "\n"
    assert result.duration_seconds >= 0
    assert report.duration_seconds >= result.duration_seconds


def test_mutation_forbidden_gate_fails_when_snapshot_changes(tmp_path):
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before")
    code = f"from pathlib import Path; Path({str(tracked)!r}).write_text('after')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    gate = _gate("read-only audit", command)

    with pytest.raises(VerificationFailed) as caught:
        run_verification_gates(
            tmp_path,
            (gate,),
            log_dir=tmp_path / "logs",
            snapshotter=lambda _path: tracked.read_text(),
        )

    result = caught.value.report.gates[0]
    assert result.status is GateStatus.MUTATION_DETECTED
    assert result.returncode == 0
    assert result.snapshot_before == "before"
    assert result.snapshot_after == "after"
    assert "mutation-forbidden" in result.error


def test_mutation_allowed_gate_does_not_require_or_call_snapshotter(tmp_path):
    changed = tmp_path / "generated.txt"
    code = f"from pathlib import Path; Path({str(changed)!r}).write_text('ok')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    gate = _gate("formatter", command, mutation="allow")

    report = run_verification_gates(
        tmp_path,
        (gate,),
        log_dir=tmp_path / "logs",
    )

    assert report.success is True
    assert changed.read_text() == "ok"
    assert report.gates[0].snapshot_before is None
    assert report.gates[0].snapshot_after is None


def test_forbidden_gate_without_snapshotter_fails_closed_before_dispatch(tmp_path):
    calls = []

    with pytest.raises(VerificationConfigurationError, match="require a snapshotter"):
        run_verification_gates(
            tmp_path,
            (_gate("lint", "lint"),),
            log_dir=tmp_path / "logs",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

    assert calls == []


def test_typed_process_failures_are_aggregated_with_log_evidence(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command == "timeout":
            Path(kwargs["stdout_log"]).write_text("timeout log")
            raise ProcessTimeoutError(
                command, kwargs["timeout"], output="partial", stderr="slow"
            )
        if command == "start":
            raise ProcessStartError(["missing"], FileNotFoundError("missing"))
        if command == "large":
            Path(kwargs["stdout_log"]).write_text("large output tail")
            raise ProcessOutputLimitError("stdout", 8)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    gates = tuple(
        _gate(name, command, mutation="allow")
        for name, command in (
            ("timeout gate", "timeout"),
            ("start gate", "start"),
            ("large gate", "large"),
            ("last gate", "pass"),
        )
    )

    with pytest.raises(VerificationFailed) as caught:
        run_verification_gates(
            tmp_path,
            gates,
            log_dir=tmp_path / "logs",
            runner=runner,
        )

    assert calls == ["timeout", "start", "large", "pass"]
    assert [gate.status for gate in caught.value.report.gates] == [
        GateStatus.TIMED_OUT,
        GateStatus.START_ERROR,
        GateStatus.OUTPUT_LIMIT,
        GateStatus.PASSED,
    ]
    assert caught.value.report.gates[0].stdout_excerpt == "partial"
    assert caught.value.report.gates[2].stdout_excerpt == "large output tail"


def test_cancellation_is_blocking_and_skips_later_commands(tmp_path):
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        raise ProcessCancelledError(command, stdout="stopped", stderr="cancelled")

    gates = (
        _gate("advisory", "cancel", required=False),
        _gate("later", "must-not-run", mutation="allow"),
    )

    with pytest.raises(VerificationFailed) as caught:
        run_verification_gates(
            tmp_path,
            gates,
            log_dir=tmp_path / "logs",
            snapshotter=lambda _path: "same",
            runner=runner,
        )

    assert calls == ["cancel"]
    assert [gate.status for gate in caught.value.report.gates] == [
        GateStatus.CANCELLED,
        GateStatus.SKIPPED,
    ]
    assert caught.value.report.gates[0].blocking is True


def test_snapshot_failure_becomes_typed_gate_evidence_without_running_command(tmp_path):
    calls = []

    def broken_snapshot(_path):
        raise OSError("snapshot unavailable")

    with pytest.raises(VerificationFailed) as caught:
        run_verification_gates(
            tmp_path,
            (_gate("audit", "audit"),),
            log_dir=tmp_path / "logs",
            snapshotter=broken_snapshot,
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

    result = caught.value.report.gates[0]
    assert calls == []
    assert result.status is GateStatus.SNAPSHOT_ERROR
    assert "snapshot unavailable" in result.error


def test_accepts_legacy_gate_through_the_resolved_config_api(tmp_path):
    config = MachinistConfig.model_validate({"tests": {"command": "legacy-test"}})
    seen = []

    def runner(command, **kwargs):
        seen.append((command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, 0, "legacy passed", "")

    report = run_verification_gates(
        tmp_path,
        config.resolved_verification_gates(),
        log_dir=tmp_path / "logs",
        runner=runner,
    )

    assert seen == [("legacy-test", config.harness_for("execute").timeout_minutes * 60)]
    assert report.gates[0].name == "legacy-tests"
    assert report.gates[0].mutation_policy == GateMutationPolicy.ALLOW.value


def _gate(
    name: str,
    command: str,
    *,
    timeout: int = 1,
    required: bool = True,
    mutation: str = "forbid",
) -> VerificationGateConfig:
    return VerificationGateConfig(
        name=name,
        command=command,
        timeout_minutes=timeout,
        required=required,
        mutation_policy=mutation,
    )

"""Tests for credential reduction and whole-process-group supervision."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from machinist.process import (
    HARNESS_CREDENTIAL_ALLOWLIST,
    ProcessCancelledError,
    ProcessOutputLimitError,
    ProcessStartError,
    ProcessStragglerError,
    ProcessTimeoutError,
    credential_reduced_environment,
    run_supervised,
)


def test_credential_policy_denies_controller_cloud_and_generic_secrets():
    source = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/Users/tester",
        "TMPDIR": "/tmp/probe",
        "LANG": "en_US.UTF-8",
        "GH_TOKEN": "github",
        "GITHUB_TOKEN": "actions",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AWS_SESSION_TOKEN": "aws-session",
        "AZURE_CLIENT_SECRET": "azure",
        "ARM_CLIENT_SECRET": "terraform-azure",
        "GCP_ACCESS_TOKEN": "gcp",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/gcp.json",
        "CLOUDSDK_AUTH_ACCESS_TOKEN": "gcloud",
        "CLOUDFLARE_API_TOKEN": "cloudflare",
        "ACME_DEPLOY_TOKEN": "generic-cloud-token",
        "ANTHROPIC_API_KEY": "provider-key",
    }

    reduced = credential_reduced_environment(
        source,
        allow=HARNESS_CREDENTIAL_ALLOWLIST,
    )

    for legitimate in ("PATH", "HOME", "TMPDIR", "LANG"):
        assert reduced[legitimate] == source[legitimate]
    for secret in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "ARM_CLIENT_SECRET",
        "GCP_ACCESS_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "ACME_DEPLOY_TOKEN",
    ):
        assert secret not in reduced
    assert reduced["ANTHROPIC_API_KEY"] == "provider-key"
    assert reduced["GIT_TERMINAL_PROMPT"] == "0"


def test_real_child_receives_reduced_environment_canary(tmp_path):
    source = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path / "tmp"),
        "GH_TOKEN": "github",
        "AWS_ACCESS_KEY_ID": "aws",
        "AZURE_CLIENT_SECRET": "azure",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/gcp.json",
        "ANTHROPIC_API_KEY": "provider",
    }
    environment = credential_reduced_environment(
        source,
        allow=HARNESS_CREDENTIAL_ALLOWLIST,
    )
    probe = (
        "import json, os; "
        "print(json.dumps({key: os.environ.get(key) for key in "
        "['PATH', 'HOME', 'TMPDIR', 'GH_TOKEN', 'AWS_ACCESS_KEY_ID', "
        "'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS', "
        "'ANTHROPIC_API_KEY', 'GIT_TERMINAL_PROMPT']}))"
    )

    result = run_supervised(
        [sys.executable, "-c", probe],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )

    child = json.loads(result.stdout)
    assert child["PATH"] == source["PATH"]
    assert child["HOME"] == source["HOME"]
    assert child["TMPDIR"] == source["TMPDIR"]
    assert child["ANTHROPIC_API_KEY"] == "provider"
    assert child["GIT_TERMINAL_PROMPT"] == "0"
    for secret in (
        "GH_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        assert child[secret] is None


def test_capture_is_memory_bounded_and_optional_logs_are_retained(tmp_path):
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    command = [
        sys.executable,
        "-c",
        "import sys; print('normal output'); print('warning', file=sys.stderr)",
    ]

    result = run_supervised(
        command,
        text=True,
        timeout=5,
        max_output_bytes=1024,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )

    assert result.stdout == "normal output\n"
    assert result.stderr == "warning\n"
    assert stdout_log.read_text() == result.stdout
    assert stderr_log.read_text() == result.stderr


def test_runner_accepts_the_existing_shell_test_gate_call_shape(tmp_path):
    test_code = "print('quality gate passed')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(test_code)}"

    result = run_supervised(
        command,
        shell=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "quality gate passed\n"


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_successful_process_tree_that_fully_exits_remains_compatible(tmp_path):
    child_code = "print('child output', flush=True)"
    parent_code = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "child.wait(); "
        "print('leader output', flush=True)"
    )

    result = run_supervised(
        [sys.executable, "-c", parent_code],
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "child output\nleader output\n"


@pytest.mark.skipif(os.name != "posix", reason="signal assertions require POSIX")
def test_supervisor_restores_prior_termination_signal_handlers():
    termination_signals = (signal.SIGTERM, signal.SIGHUP)
    prior = {
        current_signal: signal.getsignal(current_signal)
        for current_signal in termination_signals
    }

    def custom_handler(_signal_number, _frame):
        return None

    try:
        for current_signal in termination_signals:
            signal.signal(current_signal, custom_handler)

        result = run_supervised(
            [sys.executable, "-c", "print('done')"],
            text=True,
            timeout=5,
        )

        assert result.returncode == 0
        assert all(
            signal.getsignal(current_signal) is custom_handler
            for current_signal in termination_signals
        )
    finally:
        for current_signal, prior_handler in prior.items():
            signal.signal(current_signal, prior_handler)


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_successful_leader_with_background_child_fails_closed_and_cleans_group(
    tmp_path,
):
    child_pid = tmp_path / "background-child.pid"
    late_mutation = tmp_path / "late-mutation"
    stdout_log = tmp_path / "stdout.log"
    stderr_log = tmp_path / "stderr.log"
    child_code = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(0.3); "
        f"Path({str(late_mutation)!r}).write_text('escaped')"
    )
    leader_code = (
        "import subprocess, sys, time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        f"pid_path = Path({str(child_pid)!r}); "
        "deadline = time.monotonic() + 2; "
        "\nwhile not pid_path.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "print('leader output', flush=True); "
        "print('leader warning', file=sys.stderr, flush=True)"
    )

    with pytest.raises(ProcessStragglerError) as caught:
        run_supervised(
            [sys.executable, "-c", leader_code],
            text=True,
            timeout=5,
            termination_grace_seconds=0.02,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
        )

    error = caught.value
    assert error.leader_returncode == 0
    assert error.stdout == "leader output\n"
    assert error.stderr == "leader warning\n"
    assert stdout_log.read_text() == error.stdout
    assert stderr_log.read_text() == error.stderr
    _assert_tree_stopped((child_pid,))
    time.sleep(0.35)
    assert not late_mutation.exists()


def test_output_limit_fails_loudly_and_keeps_log(tmp_path):
    stdout_log = tmp_path / "oversized.log"

    with pytest.raises(ProcessOutputLimitError) as caught:
        run_supervised(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"],
            text=True,
            timeout=5,
            max_output_bytes=128,
            stdout_log=stdout_log,
        )

    assert caught.value.stream == "stdout"
    assert stdout_log.stat().st_size == 4096


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_timeout_terminates_parent_child_and_grandchild(tmp_path):
    command, pid_paths = _ignoring_process_tree(tmp_path / "timeout")

    with pytest.raises(ProcessTimeoutError) as caught:
        run_supervised(
            command,
            text=True,
            timeout=2,
            termination_grace_seconds=0.1,
        )

    assert isinstance(caught.value, subprocess.TimeoutExpired)
    _assert_tree_stopped(pid_paths)


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_cancellation_terminates_parent_child_and_grandchild(tmp_path):
    command, pid_paths = _ignoring_process_tree(tmp_path / "cancel")
    with pytest.raises(ProcessCancelledError):
        run_supervised(
            command,
            text=True,
            timeout=10,
            cancel_check=lambda: all(path.exists() for path in pid_paths),
            termination_grace_seconds=0.1,
        )

    _assert_tree_stopped(pid_paths)


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_cooperative_child_cancellation_is_typed_under_repeated_reaping_races():
    command = [sys.executable, "-c", "import time; time.sleep(60)"]

    for _ in range(10):
        with pytest.raises(ProcessCancelledError):
            run_supervised(
                command,
                text=True,
                timeout=5,
                cancel_check=lambda: True,
                termination_grace_seconds=0.02,
                poll_interval=0.001,
            )


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
def test_cooperative_child_timeout_is_typed_under_repeated_reaping_races():
    command = [sys.executable, "-c", "import time; time.sleep(60)"]

    for _ in range(10):
        with pytest.raises(ProcessTimeoutError):
            run_supervised(
                command,
                text=True,
                timeout=0,
                termination_grace_seconds=0.02,
                poll_interval=0.001,
            )


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
@pytest.mark.parametrize(
    ("payload", "max_output_bytes"),
    ((b"\xff", 1024), (b"xx", 1)),
)
def test_cancellation_evidence_errors_cannot_mask_operator_intent(
    tmp_path,
    payload,
    max_output_bytes,
):
    ready = tmp_path / "ready"
    code = (
        "import os, time; from pathlib import Path; "
        f"os.write(1, {payload!r}); "
        f"Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(60)"
    )

    deadline = time.monotonic() + 10

    def cancel_check():
        # The supervisor calls this first in every poll iteration and is
        # single threaded, so blocking here until the child has written its
        # oversized or undecodable output makes both conditions true in the
        # same iteration. Returning ready.exists() directly leaves a gap
        # between the child's write and its marker, and the output-limit
        # check can fire inside that gap on a loaded machine.
        while not ready.exists():
            assert time.monotonic() < deadline, "child never signalled readiness"
            time.sleep(0.005)
        return True

    with pytest.raises(ProcessCancelledError) as caught:
        run_supervised(
            [sys.executable, "-c", code],
            text=True,
            encoding="utf-8",
            timeout=5,
            cancel_check=cancel_check,
            max_output_bytes=max_output_bytes,
            termination_grace_seconds=0.02,
            poll_interval=0.01,
        )

    assert caught.value.stdout is None


@pytest.mark.skipif(os.name != "posix", reason="process-group assertions require POSIX")
@pytest.mark.parametrize(
    ("payload", "max_output_bytes"),
    ((b"\xff", 1024), (b"xx", 1)),
)
def test_timeout_evidence_errors_cannot_mask_deadline(
    tmp_path,
    payload,
    max_output_bytes,
):
    ready = tmp_path / "ready"
    code = (
        "import os, time; from pathlib import Path; "
        f"os.write(1, {payload!r}); "
        f"Path({str(ready)!r}).write_text('ready'); "
        "time.sleep(60)"
    )

    def wait_for_evidence() -> bool:
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() >= deadline:
                pytest.fail("child did not write interruption evidence")
            time.sleep(0.001)
        return False

    with pytest.raises(ProcessTimeoutError) as caught:
        run_supervised(
            [sys.executable, "-c", code],
            text=True,
            encoding="utf-8",
            timeout=0,
            cancel_check=wait_for_evidence,
            max_output_bytes=max_output_bytes,
            termination_grace_seconds=0.02,
            poll_interval=0.001,
        )

    assert caught.value.output is None


def test_start_failure_has_stable_typed_error(tmp_path):
    with pytest.raises(ProcessStartError, match="could not start process") as caught:
        run_supervised(
            [str(tmp_path / "missing-executable")],
            timeout=1,
            text=True,
        )

    assert isinstance(caught.value.cause, FileNotFoundError)


def test_invalid_programmatic_command_has_stable_typed_start_error():
    with pytest.raises(ProcessStartError, match="could not start process") as caught:
        run_supervised(["invalid\x00command"], timeout=1, text=True)

    assert isinstance(caught.value.cause, ValueError)


@pytest.mark.skipif(os.name != "posix", reason="signal assertions require POSIX")
@pytest.mark.parametrize(
    ("termination_signal", "expected_exit"),
    (
        (signal.SIGTERM, 128 + signal.SIGTERM),
        (signal.SIGHUP, 128 + signal.SIGHUP),
    ),
)
def test_controller_signal_cleans_process_tree_and_finishes_lifecycle(
    tmp_path,
    termination_signal,
    expected_exit,
):
    runs_dir = tmp_path / "runs"
    leader_pid = tmp_path / "leader.pid"
    descendant_pid = tmp_path / "descendant.pid"
    stdout_log = tmp_path / "signal.stdout.log"
    stderr_log = tmp_path / "signal.stderr.log"
    descendant_code = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        f"Path({str(descendant_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    leader_code = (
        "import os, signal, subprocess, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        f"Path({str(leader_pid)!r}).write_text(str(os.getpid())); "
        "os.write(1, b'\\xff'); "
        f"subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); "
        f"descendant = Path({str(descendant_pid)!r}); "
        "deadline = time.monotonic() + 2; "
        "\nwhile not descendant.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        "time.sleep(60)"
    )
    worker_code = (
        "import sys; from pathlib import Path; "
        "from machinist.lifecycle import Phase, TaskLifecycle; "
        "from machinist.process import run_supervised; "
        f"lifecycle = TaskLifecycle(Path({str(runs_dir)!r})); "
        "lifecycle.run(42, Phase.EXECUTE, lambda _claim: run_supervised("
        f"[sys.executable, '-c', {leader_code!r}], "
        f"stdout_log={str(stdout_log)!r}, stderr_log={str(stderr_log)!r}, "
        "timeout=30, text=True, encoding='utf-8', termination_grace_seconds=0.05))"
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
            projection.exists() and leader_pid.exists() and descendant_pid.exists()
        ):
            if worker.poll() is not None:
                break
            time.sleep(0.02)
        assert projection.exists() and leader_pid.exists() and descendant_pid.exists()

        worker.send_signal(termination_signal)
        stdout, stderr = worker.communicate(timeout=5)

        assert worker.returncode == expected_exit
        assert stdout == ""
        assert "Traceback" not in stderr
        assert stdout_log.read_bytes() == b"\xff"
        assert stderr_log.read_bytes() == b""
        _assert_tree_stopped((leader_pid, descendant_pid))
        payload = json.loads(projection.read_text())
        assert payload["status"] == "cancelled"
        assert payload["ended_at"] is not None
        assert payload["error"] in {
            "process interrupted by SIGTERM",
            "process interrupted by SIGHUP",
        }
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
        if leader_pid.exists():
            try:
                os.killpg(int(leader_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_supervisor_refuses_symlinked_output_log_without_clobbering_target(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    stdout_log = tmp_path / "stdout.log"
    stdout_log.symlink_to(victim)

    with pytest.raises(ProcessStartError, match="symlink or non-regular"):
        run_supervised(
            [sys.executable, "-c", "print('CLOBBER')"],
            capture_output=True,
            text=True,
            stdout_log=stdout_log,
        )

    assert victim.read_text() == "keep\n"


def _ignoring_process_tree(directory: Path) -> tuple[list[str], tuple[Path, ...]]:
    directory.mkdir(parents=True)
    parent_pid = directory / "parent.pid"
    child_pid = directory / "child.pid"
    grandchild_pid = directory / "grandchild.pid"
    grandchild_code = (
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(grandchild_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    child_code = (
        "import os, signal, subprocess, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}]); "
        "time.sleep(60)"
    )
    parent_code = (
        "import os, signal, subprocess, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(parent_pid)!r}).write_text(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(60)"
    )
    return [sys.executable, "-c", parent_code], (parent_pid, child_pid, grandchild_pid)


def _assert_tree_stopped(pid_paths: tuple[Path, ...]) -> None:
    assert all(path.exists() for path in pid_paths), (
        "process tree did not finish starting"
    )
    pids = [int(path.read_text()) for path in pid_paths]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and any(_process_is_live(pid) for pid in pids):
        time.sleep(0.05)
    assert not [pid for pid in pids if _process_is_live(pid)]


def _process_is_live(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    state = result.stdout.strip()
    return bool(state) and not state.startswith("Z")

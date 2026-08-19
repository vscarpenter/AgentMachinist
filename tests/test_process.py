"""Tests for credential reduction and whole-process-group supervision."""

from __future__ import annotations

import json
import os
import shlex
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


def test_start_failure_has_stable_typed_error(tmp_path):
    with pytest.raises(ProcessStartError, match="could not start process") as caught:
        run_supervised(
            [str(tmp_path / "missing-executable")],
            timeout=1,
            text=True,
        )

    assert isinstance(caught.value.cause, FileNotFoundError)


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

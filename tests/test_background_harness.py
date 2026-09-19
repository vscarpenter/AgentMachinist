"""Container-only Codex profile, with filesystem policy enforced by Docker."""

import subprocess
import sys
import threading

import pytest

from machinist.background_harness import get_background_harness
from machinist.background_runtime import ContainerRuntime
from machinist.config import HarnessConfig
from machinist.harness import get_harness
from machinist.harness.base import HarnessCancelledError, HarnessError


@pytest.fixture
def runtime(tmp_path):
    controller = tmp_path / "controller"
    controller.mkdir()
    workshop = tmp_path / "workshops" / "T1"
    (workshop / ".git").mkdir(parents=True)
    (workshop / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "response", "")

    runtime = ContainerRuntime(
        "worker:test",
        controller_root=controller,
        workshop_root=tmp_path / "workshops",
        runner=runner,
    )
    return runtime, workshop, calls


@pytest.mark.parametrize("phase", ["spec", "execute", "review"])
def test_background_profile_enforces_phase_mount_and_avoids_nested_sandbox(
    runtime, phase
):
    boundary, workshop, calls = runtime
    config = HarnessConfig(name="codex", model="configured-model")
    harness = get_background_harness(config, runtime=boundary, phase=phase)
    method = {"spec": "generate_spec", "execute": "implement", "review": "review"}[
        phase
    ]

    assert getattr(harness, method)("task prompt", workshop) == "response"

    argv, _ = calls[0]
    assert argv[:2] == ["docker", "run"]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert 'approval_policy="never"' in argv
    assert "--ephemeral" in argv
    assert "configured-model" in argv
    assert argv[-1] == "task prompt"
    assert argv[argv.index("--mount") + 1].endswith(",readonly") == (phase != "execute")


def test_background_profile_does_not_change_host_codex_adapter(runtime):
    boundary, _, _ = runtime
    config = HarnessConfig(name="codex")
    get_background_harness(config, runtime=boundary, phase="execute")
    ordinary = get_harness(config)
    assert "workspace-write" in ordinary.implement_argv("task")
    assert "read-only" in ordinary.spec_argv("task")
    assert "danger-full-access" not in ordinary.implement_argv("task")


def test_background_harness_requires_supported_adapter_and_explicit_phase(runtime):
    boundary, _, _ = runtime
    with pytest.raises(HarnessError, match="Codex"):
        get_background_harness(
            HarnessConfig(name="claude-code"), runtime=boundary, phase="execute"
        )
    with pytest.raises(HarnessError, match="Phase"):
        get_background_harness(
            HarnessConfig(name="codex"), runtime=boundary, phase="unknown"
        )


def test_background_harness_rejects_arbitrary_host_runner():
    with pytest.raises(HarnessError, match="ContainerRuntime"):
        get_background_harness(
            HarnessConfig(name="codex"), runtime=subprocess.run, phase="execute"
        )


def test_execute_mount_cannot_be_reused_for_a_read_only_phase(runtime):
    boundary, workshop, calls = runtime
    harness = get_background_harness(
        HarnessConfig(name="codex"), runtime=boundary, phase="execute"
    )
    with pytest.raises(HarnessError, match="Phase does not match its mount policy"):
        harness.generate_spec("plan", workshop)
    with pytest.raises(HarnessError, match="Phase does not match its mount policy"):
        harness.review("review", workshop)
    assert calls == []


def test_background_harness_propagates_current_repair_cancellation(runtime):
    boundary, workshop, calls = runtime
    harness = get_background_harness(
        HarnessConfig(name="codex"), runtime=boundary, phase="execute"
    )
    harness.on_progress = lambda message: None
    # Repair replaces the callback after Harness construction. The current
    # bounded callback must reach Docker, not just the outer Task deadline.
    harness.cancel_check = lambda: True
    with pytest.raises(HarnessCancelledError):
        harness.implement("repair within its consumed budget", workshop)
    assert not calls


def test_progress_keeps_container_supervision_on_the_main_thread(runtime):
    boundary, workshop, calls = runtime
    original_runner = boundary._runner
    threads = []

    def runner(argv, **kwargs):
        threads.append(threading.current_thread())
        if argv[:2] == ["docker", "run"]:
            assert callable(kwargs.get("progress_callback"))
        return original_runner(argv, **kwargs)

    boundary._runner = runner
    harness = get_background_harness(
        HarnessConfig(name="codex"), runtime=boundary, phase="spec"
    )
    harness.on_progress = lambda message: None
    harness.generate_spec("plan", workshop)
    assert threads and all(thread is threading.main_thread() for thread in threads)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX worker signal contract")
def test_sigint_interrupts_active_container_and_runs_cleanup(tmp_path):
    script = r"""
import os, signal, subprocess, sys, threading, time
from pathlib import Path
from machinist.background_harness import get_background_harness
from machinist.background_runtime import ContainerRuntime
from machinist.config import HarnessConfig
from machinist.process import run_supervised
root = Path(sys.argv[1]).resolve()
controller = root / 'controller'
controller.mkdir()
workshop = root / 'workshops' / 'task'
(workshop / '.git').mkdir(parents=True)
(workshop / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
calls = []
def runner(argv, **kwargs):
    calls.append(argv[1])
    if argv[1] == 'run':
        timer = threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGINT))
        timer.start()
        try:
            return run_supervised([sys.executable, '-c', 'import time; time.sleep(8)'], **kwargs)
        finally:
            timer.cancel()
    return subprocess.CompletedProcess(argv, 0, '', '')
runtime = ContainerRuntime('worker:test', controller_root=controller, workshop_root=root/'workshops', runner=runner)
harness = get_background_harness(HarnessConfig(name='codex'), runtime=runtime, phase='spec')
harness.on_progress = lambda message: None
started = time.monotonic()
try:
    harness.generate_spec('plan', workshop)
except KeyboardInterrupt:
    assert time.monotonic() - started < 4
    assert calls == ['run', 'rm'], calls
else:
    raise AssertionError('SIGINT was not propagated')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=12,
    )
    assert result.returncode == 0, result.stderr

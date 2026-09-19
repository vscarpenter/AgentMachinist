"""Contracts for the disposable background execution boundary (no Docker needed)."""

import subprocess

import pytest

from machinist.background_runtime import ContainerRuntime
from machinist.config import VerificationGateConfig
from machinist.process import (
    ProcessCancelledError,
    ProcessOutputLimitError,
    ProcessStartError,
)
from machinist.verification import VerificationFailed, run_verification_gates


class DockerRunner:
    def __init__(self):
        self.calls = []
        self.failure = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1] == "run" and self.failure:
            raise self.failure
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")


@pytest.fixture
def runtime_paths(tmp_path):
    controller = tmp_path / "controller"
    controller.mkdir()
    workshops = tmp_path / "workshops"
    workshop = workshops / "T1"
    (workshop / ".git" / "objects").mkdir(parents=True)
    (workshop / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return controller, workshops, workshop


def make_runtime(runtime_paths, runner, **kwargs):
    controller, workshops, _ = runtime_paths
    return ContainerRuntime(
        "machinist-worker:test",
        controller_root=controller,
        workshop_root=workshops,
        runner=runner,
        **kwargs,
    )


def option(argv, name):
    return argv[argv.index(name) + 1]


def test_mounts_only_clone_and_enforces_execution_boundary(runtime_paths):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner, network="none")
    workshop = runtime_paths[2]
    completed = runtime.gate_runner("python -m pytest", shell=True, cwd=workshop)
    argv, kwargs = runner.calls[0]
    assert completed.returncode == 0
    assert argv[:2] == ["docker", "run"]
    assert argv.count("--mount") == 1
    assert option(argv, "--mount") == f"type=bind,src={workshop},dst={workshop}"
    assert option(argv, "--workdir") == str(workshop)
    assert "--read-only" in argv
    assert option(argv, "--cap-drop") == "ALL"
    assert option(argv, "--security-opt") == "no-new-privileges=true"
    assert option(argv, "--network") == "none"
    assert option(argv, "--pids-limit") == "256"
    assert option(argv, "--memory") == "4g"
    assert option(argv, "--memory-swap") == "4g"
    assert "--privileged" not in argv
    assert "--volumes-from" not in argv
    assert kwargs["shell"] is False
    assert kwargs["max_output_bytes"] == 8 * 1024 * 1024
    assert argv[-3:] == ["/bin/sh", "-c", "python -m pytest"]
    assert runner.calls[-1][0] == ["docker", "rm", "--force", option(argv, "--name")]


def test_harness_only_gets_selected_provider_key_and_not_host_environment(
    runtime_paths,
):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner, credential_names=("OPENAI_API_KEY",))
    environment = {
        "HOME": "/host/private-home",
        "PATH": "/host/private/bin",
        "OPENAI_API_KEY": "provider-secret",
        "ANTHROPIC_API_KEY": "unselected-secret",
        "GH_TOKEN": "publication-secret",
        "GITHUB_TOKEN": "other-publication-secret",
        "AWS_SECRET_ACCESS_KEY": "cloud-secret",
    }
    runtime.harness_runner(
        ["codex", "exec", "test"], cwd=runtime_paths[2], env=environment
    )
    argv, kwargs = runner.calls[0]
    assert "OPENAI_API_KEY" in argv
    assert "CODEX_API_KEY" in argv
    assert kwargs["env"]["CODEX_API_KEY"] == "provider-secret"
    assert "provider-secret" not in " ".join(argv)
    assert "HOME=/tmp/machinist-home" in argv
    for forbidden in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert forbidden not in argv
        assert forbidden not in kwargs["env"]
    assert environment["HOME"] not in " ".join(argv)
    assert environment["PATH"] not in " ".join(argv)
    runtime.gate_runner("true", shell=True, cwd=runtime_paths[2], env=environment)
    gate_argv, gate_kwargs = runner.calls[2]
    assert "OPENAI_API_KEY" not in gate_argv
    assert "CODEX_API_KEY" not in gate_argv
    assert "OPENAI_API_KEY" not in gate_kwargs["env"]


@pytest.mark.parametrize(
    "kind", ["controller", "outside", "worktree", "symlink", "alternates"]
)
def test_rejects_unsafe_mount_before_any_docker_process(runtime_paths, tmp_path, kind):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner)
    controller, workshops, workshop = runtime_paths
    cwd = workshop
    if kind == "controller":
        cwd = controller
    elif kind == "outside":
        cwd = tmp_path
    elif kind == "worktree":
        (workshop / ".git" / "HEAD").unlink()
        (workshop / ".git" / "objects").rmdir()
        (workshop / ".git").rmdir()
        (workshop / ".git").write_text(f"gitdir: {controller}/.git/worktrees/T1\n")
    elif kind == "symlink":
        cwd = workshops / "alias"
        cwd.symlink_to(workshop, target_is_directory=True)
    else:
        (workshop / ".git" / "objects" / "info").mkdir()
        (workshop / ".git" / "objects" / "info" / "alternates").write_text(
            str(controller)
        )
    with pytest.raises(ProcessStartError, match="Workshop|clone|symlink|alternates"):
        runtime.gate_runner("true", shell=True, cwd=cwd)
    assert runner.calls == []


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired("docker", 3),
        ProcessCancelledError("docker"),
        KeyboardInterrupt(),
    ],
)
def test_removes_container_on_all_interruption_paths(runtime_paths, failure):
    runner = DockerRunner()
    runner.failure = failure
    runtime = make_runtime(runtime_paths, runner)
    with pytest.raises(type(failure)):
        runtime.gate_runner("sleep 99", shell=True, cwd=runtime_paths[2], timeout=3)
    assert runner.calls[-1][0][:3] == ["docker", "rm", "--force"]
    assert runner.calls[-1][1]["timeout"] <= 15
    assert runner.calls[-1][1].get("cancel_check") is None


def test_shared_deadline_bounds_host_and_container_execution(runtime_paths):
    runner = DockerRunner()
    runtime = make_runtime(
        runtime_paths, runner, deadline=lambda: 112.5, clock=lambda: 100
    )
    runtime.gate_runner("true", shell=True, cwd=runtime_paths[2], timeout=900)
    argv, kwargs = runner.calls[0]
    assert kwargs["timeout"] == 12.5
    assert "/usr/bin/timeout" in argv
    assert "12.5s" in argv


def test_expired_or_cancelled_task_cannot_start(runtime_paths):
    runner = DockerRunner()
    expired = make_runtime(runtime_paths, runner, deadline=lambda: 10, clock=lambda: 11)
    with pytest.raises(subprocess.TimeoutExpired):
        expired.gate_runner("true", shell=True, cwd=runtime_paths[2])
    cancelled = make_runtime(runtime_paths, runner, cancel_check=lambda: True)
    with pytest.raises(ProcessCancelledError):
        cancelled.harness_runner(["codex"], cwd=runtime_paths[2])
    assert runner.calls == []


def test_probe_is_bounded_and_never_pulls_or_runs_image(runtime_paths):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner)
    assert runtime.probe().ready
    assert [call[0][1:3] for call in runner.calls] == [
        ["info", "--format"],
        ["image", "inspect"],
    ]
    assert all(call[1]["timeout"] <= 15 for call in runner.calls)


def test_probe_failure_does_not_fall_back_to_host(runtime_paths):
    calls = []

    def unavailable(argv, **kwargs):
        calls.append(argv)
        raise FileNotFoundError("docker")

    runtime = make_runtime(runtime_paths, unavailable)
    result = runtime.probe()
    assert not result.ready
    assert "Docker" in result.detail
    with pytest.raises(FileNotFoundError):
        runtime.gate_runner("python -m pytest", shell=True, cwd=runtime_paths[2])
    assert all(argv[0] == "docker" for argv in calls)


def test_log_paths_stay_controller_side(runtime_paths, tmp_path):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner)
    stdout_log = tmp_path / "state" / "stdout.log"
    runtime.gate_runner("true", shell=True, cwd=runtime_paths[2], stdout_log=stdout_log)
    argv, kwargs = runner.calls[0]
    assert kwargs["stdout_log"] == stdout_log
    assert str(stdout_log) not in " ".join(argv)


def test_cleanup_failure_cannot_report_success(runtime_paths):
    def failed_cleanup(argv, **kwargs):
        code = 1 if argv[1] == "rm" else 0
        return subprocess.CompletedProcess(argv, code, "", "daemon unavailable")

    runtime = make_runtime(runtime_paths, failed_cleanup)
    with pytest.raises(ProcessStartError, match="cleanup"):
        runtime.gate_runner("true", shell=True, cwd=runtime_paths[2])


def test_cleanup_stale_targets_only_this_worker_label(runtime_paths):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        stdout = "012345abcdef\n" if argv[1] == "ps" else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    runtime = make_runtime(runtime_paths, runner)
    runtime.cleanup_stale()
    assert calls[0][:4] == ["docker", "ps", "--all", "--quiet"]
    assert option(calls[0], "--filter").startswith(
        "label=dev.agentmachinist.controller="
    )
    assert calls[1] == ["docker", "rm", "--force", "012345abcdef"]


@pytest.mark.parametrize("code,output", [(1, ""), (0, "--all")])
def test_stale_cleanup_fails_closed_on_unknown_inventory(runtime_paths, code, output):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, output, "")

    with pytest.raises(ProcessStartError):
        make_runtime(runtime_paths, runner).cleanup_stale()
    assert len(calls) == 1


def test_runtime_timeout_flows_into_gate_evidence(runtime_paths, tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 124 if argv[1] == "run" else 0, "partial", ""
        )

    runtime = make_runtime(runtime_paths, runner)
    with pytest.raises(VerificationFailed) as failure:
        run_verification_gates(
            runtime_paths[2],
            [VerificationGateConfig(name="tests", command="sleep 10")],
            log_dir=tmp_path / "logs",
            runner=runtime.gate_runner,
            snapshotter=lambda _: "unchanged",
        )
    gate = failure.value.report.gates[0]
    assert gate.status.value == "timed_out"
    assert gate.stdout_excerpt == "partial"


def test_cleanup_failure_flows_into_gate_evidence(runtime_paths, tmp_path):
    def runner(argv, **kwargs):
        if argv[1] == "rm":
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, 0, "", "")

    runtime = make_runtime(runtime_paths, runner)
    with pytest.raises(VerificationFailed) as failure:
        run_verification_gates(
            runtime_paths[2],
            [VerificationGateConfig(name="tests", command="true")],
            log_dir=tmp_path / "logs",
            runner=runtime.gate_runner,
            snapshotter=lambda _: "unchanged",
        )
    assert failure.value.report.gates[0].status.value == "start_error"


def test_output_limit_still_removes_container(runtime_paths):
    runner = DockerRunner()
    runner.failure = ProcessOutputLimitError("stdout", 1024)
    with pytest.raises(ProcessOutputLimitError):
        make_runtime(runtime_paths, runner).gate_runner(
            "true", shell=True, cwd=runtime_paths[2]
        )
    assert runner.calls[-1][0][1] == "rm"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"image": "--evil"},
        {"network": "host"},
        {"cpus": 0},
        {"memory": "0g"},
        {"credential_names": ("GH_TOKEN",)},
    ],
)
def test_rejects_unsafe_runtime_configuration(runtime_paths, kwargs):
    controller, workshops, _ = runtime_paths
    settings = {
        "image": "worker:test",
        "controller_root": controller,
        "workshop_root": workshops,
    }
    settings.update(kwargs)
    with pytest.raises(ValueError):
        ContainerRuntime(**settings)


def test_missing_image_is_readiness_failure(runtime_paths):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1 if argv[1] == "image" else 0, "", "not found"
        )

    result = make_runtime(runtime_paths, runner).probe()
    assert not result.ready
    assert "build or pull" in result.detail


def test_incoming_cancel_callback_is_supervised_together_with_worker(runtime_paths):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner, cancel_check=lambda: False)
    state = {"cancelled": False}
    runtime.gate_runner(
        "true",
        shell=True,
        cwd=runtime_paths[2],
        cancel_check=lambda: state["cancelled"],
    )
    cancellation = runner.calls[0][1]["cancel_check"]
    assert not cancellation()
    state["cancelled"] = True
    assert cancellation()


def test_read_only_harness_mount_is_enforced_by_docker(runtime_paths):
    runner = DockerRunner()
    runtime = make_runtime(runtime_paths, runner)
    runtime.harness_runner(
        ["codex", "exec", "plan"],
        cwd=runtime_paths[2],
        read_only_workshop=True,
    )
    argv, kwargs = runner.calls[0]
    assert option(argv, "--mount").endswith(",readonly")
    assert "read_only_workshop" not in kwargs

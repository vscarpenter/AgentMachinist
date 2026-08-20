"""Safe per-repository launchd service management."""

import os
import plistlib
import re
import stat
import subprocess

import pytest

from machinist.service import (
    LOG_TAIL_OUTPUT_LIMIT_BYTES,
    LOG_TAIL_READ_LIMIT_BYTES,
    LOG_TAIL_TRUNCATION_MARKER,
    LaunchdService,
    ServiceCommandError,
    ServiceError,
    read_log_tail,
    service_identifier,
)


class FakeRunner:
    def __init__(self, *results):
        self.calls = []
        self._results = list(results)

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if not self._results:
            raise AssertionError(f"unexpected service command: {argv}")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        stdout, returncode, stderr = result
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _service(
    tmp_path,
    *,
    runner=None,
    repo_name="demo",
    path_environment=None,
    start_interval=60,
):
    repo = tmp_path / repo_name
    repo.mkdir()
    executable = tmp_path / "bin" / "machinist"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    agents = tmp_path / "Library" / "LaunchAgents"
    return LaunchdService(
        repo,
        executable,
        launch_agents_dir=agents,
        uid=501,
        runner=runner or FakeRunner(),
        path_environment=path_environment,
        start_interval=start_interval,
    )


def test_identifier_is_stable_safe_and_unique_per_repository(tmp_path):
    first = tmp_path / "one" / "My Project!"
    second = tmp_path / "two" / "My Project!"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    first_label = service_identifier(first)

    assert first_label == service_identifier(first)
    assert first_label != service_identifier(second)
    assert re.fullmatch(r"[a-z0-9.-]+", first_label)
    assert "my-project" in first_label
    assert len(first_label) <= 127


def test_plist_uses_structured_escaping_absolute_paths_and_repo_cwd(tmp_path):
    service = _service(
        tmp_path,
        repo_name='demo & <repo> "quoted"',
        path_environment="/opt/homebrew/bin:/usr/bin",
    )

    encoded = service.plist_bytes()
    payload = plistlib.loads(encoded)

    assert payload["Label"] == service.label
    assert payload["ProgramArguments"] == [
        str(service.executable),
        "watch",
        "--once",
    ]
    assert service.executable.is_absolute()
    assert payload["WorkingDirectory"] == str(service.repo_root)
    assert payload["StandardOutPath"] == str(service.stdout_log_path)
    assert payload["StandardErrorPath"] == str(service.stderr_log_path)
    assert service.logs_dir == service.repo_root / ".machinist" / "runs" / "service"
    assert payload["EnvironmentVariables"] == {
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONUNBUFFERED": "1",
    }
    assert payload["StartInterval"] == 60
    assert payload["ExitTimeOut"] == 30
    assert payload["Umask"] == 0o077
    for omitted in ("RunAtLoad", "KeepAlive", "ProcessType", "Program", "Disabled"):
        assert omitted not in payload
    assert b"&amp;" in encoded


def test_constructing_and_rendering_service_never_invokes_launchctl(tmp_path):
    runner = FakeRunner()
    service = _service(tmp_path, runner=runner)

    service.plist_payload()
    service.plist_bytes()

    assert runner.calls == []


def test_management_service_needs_no_current_controller_executable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = FakeRunner(("", 113, "Could not find service"))

    service = LaunchdService.for_management(
        repo,
        launch_agents_dir=tmp_path / "Library" / "LaunchAgents",
        uid=501,
        runner=runner,
    )

    assert service.executable is None
    assert service.status().loaded is False
    with pytest.raises(ServiceError, match="management-only service cannot install"):
        service.install()
    assert runner.calls[0][0][1] == "print"


def test_log_tail_bounds_sparse_single_line_and_replaces_invalid_utf8(tmp_path):
    log = tmp_path / "large.log"
    with log.open("wb") as stream:
        stream.seek(LOG_TAIL_READ_LIMIT_BYTES * 8)
        stream.write(b"x" * (LOG_TAIL_READ_LIMIT_BYTES * 2))
        stream.write(b"\xfftail")

    tail = read_log_tail(log, lines=1)

    assert tail.truncated is True
    assert tail.bytes_read == LOG_TAIL_READ_LIMIT_BYTES
    assert tail.text.startswith(LOG_TAIL_TRUNCATION_MARKER + "\n")
    assert tail.text.endswith("\ufffdtail")
    assert len(tail.text.encode("utf-8")) <= LOG_TAIL_OUTPUT_LIMIT_BYTES


def test_log_tail_applies_line_limit_with_explicit_marker(tmp_path):
    log = tmp_path / "multiline.log"
    log.write_bytes(b"first\nsecond\nthird\n")

    tail = read_log_tail(log, lines=2)

    assert tail.truncated is True
    assert tail.bytes_read == len(log.read_bytes())
    assert tail.text == f"{LOG_TAIL_TRUNCATION_MARKER}\nsecond\nthird"


def test_install_atomically_replaces_plist_without_following_target_symlink(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    service.plist_path.parent.mkdir(parents=True)
    victim = tmp_path / "must-not-change"
    victim.write_text("keep\n")
    service.plist_path.symlink_to(victim)
    replacements = []
    real_replace = os.replace

    def record_replace(source, target):
        replacements.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr("machinist.service.os.replace", record_replace)

    installed = service.install()

    assert installed == service.plist_path
    assert victim.read_text() == "keep\n"
    assert not installed.is_symlink()
    assert plistlib.loads(installed.read_bytes())["Label"] == service.label
    assert stat.S_IMODE(installed.stat().st_mode) == 0o644
    assert service.logs_dir.is_dir()
    assert replacements == [(replacements[0][0], service.plist_path)]
    temporary = service.plist_path.parent / os.path.basename(replacements[0][0])
    assert temporary.parent == service.plist_path.parent
    assert not temporary.exists()


def test_failed_atomic_replace_preserves_previous_plist_and_cleans_temp(
    tmp_path, monkeypatch
):
    service = _service(tmp_path)
    service.plist_path.parent.mkdir(parents=True)
    service.plist_path.write_text("previous plist\n")

    def fail_replace(source, target):
        raise OSError("disk failure")

    monkeypatch.setattr("machinist.service.os.replace", fail_replace)

    with pytest.raises(ServiceError, match="disk failure"):
        service.install()

    assert service.plist_path.read_text() == "previous plist\n"
    assert list(service.plist_path.parent.glob(f".{service.label}.*.tmp")) == []


def test_rejects_relative_or_non_executable_programs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = tmp_path / "agents"

    with pytest.raises(ServiceError, match="absolute"):
        LaunchdService(repo, "machinist", launch_agents_dir=agents)

    missing = tmp_path / "missing"
    with pytest.raises(ServiceError, match="executable file"):
        LaunchdService(repo, missing, launch_agents_dir=agents)

    program = tmp_path / "program"
    program.write_text("no execute bit")
    program.chmod(0o600)
    with pytest.raises(ServiceError, match="not executable"):
        LaunchdService(repo, program, launch_agents_dir=agents)


def test_rejects_ambiguous_arguments_and_relative_launchd_path_entries(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ServiceError, match="arguments must be a sequence"):
        LaunchdService(
            service.repo_root,
            service.executable,
            launch_agents_dir=service.launch_agents_dir,
            arguments="watch",
        )
    with pytest.raises(ServiceError, match="must all be absolute"):
        LaunchdService(
            service.repo_root,
            service.executable,
            launch_agents_dir=service.launch_agents_dir,
            path_environment="relative:/usr/bin",
        )


@pytest.mark.parametrize("interval", [0, 9, True])
def test_start_interval_must_be_at_least_ten_seconds(tmp_path, interval):
    with pytest.raises(ServiceError, match="at least 10 seconds"):
        _service(tmp_path, start_interval=interval)


def test_rejects_log_paths_that_escape_repository(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ServiceError, match="logs directory must be contained"):
        LaunchdService(
            service.repo_root,
            service.executable,
            launch_agents_dir=service.plist_path.parent,
            logs_dir=tmp_path / "outside-logs",
        )

    outside = tmp_path / "symlink-target"
    outside.mkdir()
    machinist_state = service.repo_root / ".machinist"
    machinist_state.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ServiceError, match="logs directory must be contained"):
        LaunchdService(
            service.repo_root,
            service.executable,
            launch_agents_dir=service.plist_path.parent,
        )


def test_install_rejects_symlinked_service_log_leaf_without_clobbering_target(
    tmp_path,
):
    service = _service(tmp_path)
    service.logs_dir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n")
    service.stdout_log_path.symlink_to(victim)

    with pytest.raises(ServiceError, match="service log file is unsafe"):
        service.install()

    assert victim.read_text() == "keep\n"


def test_bootstrap_and_bootout_use_safe_gui_domain_argv(tmp_path):
    runner = FakeRunner(("bootstrapped", 0, ""), ("", 0, ""))
    service = _service(tmp_path, runner=runner)
    service.install()

    started = service.bootstrap()
    stopped = service.bootout()

    assert started.argv == (
        str(service.launchctl_path),
        "bootstrap",
        "gui/501",
        str(service.plist_path),
    )
    assert stopped.argv == (
        str(service.launchctl_path),
        "bootout",
        f"gui/501/{service.label}",
    )
    for _, kwargs in runner.calls:
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": 15,
            "check": False,
        }


def test_status_uses_launchctl_print_and_preserves_human_output(tmp_path):
    runner = FakeRunner(("state = running\n\tpid = 4321\n", 0, ""))
    service = _service(tmp_path, runner=runner)
    service.install()

    status = service.status()

    assert status.installed is True
    assert status.loaded is True
    assert status.output == "state = running\n\tpid = 4321\n"
    assert status.error is None
    assert runner.calls[0][0] == [
        str(service.launchctl_path),
        "print",
        f"gui/501/{service.label}",
    ]


def test_status_reports_unavailable_service_without_raising(tmp_path):
    runner = FakeRunner(("", 113, "Could not find service"))
    service = _service(tmp_path, runner=runner)

    status = service.status()

    assert status.installed is False
    assert status.loaded is False
    assert status.returncode == 113
    assert status.error == "Could not find service"


def test_start_kickstarts_registered_job_without_shell_or_output_parsing(tmp_path):
    runner = FakeRunner(("", 0, ""))
    service = _service(tmp_path, runner=runner)

    result = service.start()

    assert result.argv == (
        str(service.launchctl_path),
        "kickstart",
        f"gui/501/{service.label}",
    )


def test_restart_uses_kickstart_k(tmp_path):
    runner = FakeRunner(("", 0, ""))
    service = _service(tmp_path, runner=runner)

    result = service.restart()

    assert result.argv == (
        str(service.launchctl_path),
        "kickstart",
        "-k",
        f"gui/501/{service.label}",
    )


def test_stop_is_idempotent_when_service_is_not_loaded(tmp_path):
    runner = FakeRunner(("", 113, "Could not find service"))
    service = _service(tmp_path, runner=runner)

    result = service.stop()

    assert result.returncode == 113
    assert result.argv[1] == "bootout"


def test_uninstall_boots_out_before_removing_plist_and_preserves_logs(tmp_path):
    runner = FakeRunner(("", 0, ""))
    service = _service(tmp_path, runner=runner)
    service.install()
    service.stdout_log_path.write_text("diagnostic\n")

    removed = service.uninstall()

    assert removed is True
    assert not service.plist_path.exists()
    assert service.stdout_log_path.read_text() == "diagnostic\n"
    assert runner.calls[0][0][1] == "bootout"


def test_uninstall_preserves_plist_when_bootout_fails(tmp_path):
    runner = FakeRunner(("", 1, "permission denied"))
    service = _service(tmp_path, runner=runner)
    service.install()

    with pytest.raises(ServiceCommandError, match="permission denied"):
        service.uninstall()

    assert service.plist_path.exists()


def test_command_failures_are_typed_and_include_bounded_diagnostics(tmp_path):
    runner = FakeRunner(("", 5, "launchd refused the plist"))
    service = _service(tmp_path, runner=runner)
    service.install()

    with pytest.raises(ServiceCommandError, match="launchd refused the plist") as info:
        service.bootstrap()

    assert info.value.returncode == 5
    assert info.value.argv[1] == "bootstrap"


def test_runner_start_failures_become_service_errors(tmp_path):
    service = _service(tmp_path, runner=FakeRunner(FileNotFoundError("launchctl")))
    service.install()

    with pytest.raises(ServiceError, match="could not run launchctl"):
        service.bootstrap()

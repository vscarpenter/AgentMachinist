"""Local readiness diagnoses real Git repositories without adopting or running Tasks."""

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from machinist.config import WorkspaceConfig
from machinist.doctor import CheckLevel
from machinist.harness import HarnessRegistry
from machinist.harness.codex import Codex
from machinist.local_doctor import (
    local_fix_hint_for_check_name,
    run_local_doctor,
)
from machinist.local_setup import ensure_local_config
from machinist.workspace import Workspace


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    original_expanduser = Path.expanduser
    monkeypatch.setattr(
        Path,
        "expanduser",
        lambda path: (
            tmp_path / "workshops"
            if str(path) == "~/.machinist/workspaces"
            else original_expanduser(path)
        ),
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Local Developer")
    git(root, "config", "user.email", "local@example.com")
    (root / "pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0"\ndependencies=["pytest"]\n'
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    return root


def tree(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def which(name):
    return f"/bin/{name}" if name in {"git", "codex", "python", "pytest"} else None


def probe_runner(calls):
    def runner(args, **kwargs):
        calls.append(args)
        if args[0] == "git":
            assert not set(args) & {"fetch", "ls-remote", "push", "worktree", "clone"}
            return subprocess.run(args, **kwargs)
        assert args[0] == "codex"
        assert (
            args == ["codex", "--version"]
            or args == ["codex", "login", "status"]
            or "--help" in args
        )
        return subprocess.CompletedProcess(args, 0, "codex 1.0; logged in", "")

    return runner


def checks_by_name(report):
    return {check.name: check for check in report.checks}


def test_pre_adoption_readiness_is_no_origin_no_task_no_writes_and_no_gates(repo):
    before = tree(repo)
    calls = []
    report = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner(calls),
        gate_runner=lambda *a, **k: pytest.fail("gates require opt-in"),
    )

    assert report.ok, report.to_dict()
    checks = checks_by_name(report)
    assert checks["local configuration"].level is CheckLevel.PASS
    assert "not saved" in checks["local configuration"].detail
    assert checks["Review Harness compatibility"].level is CheckLevel.PASS
    assert checks["verification commands"].level is CheckLevel.PASS
    assert "not run" in checks["verification execution"].detail
    assert tree(repo) == before
    assert git(repo, "remote") == ""
    assert not (repo / ".machinist").exists()
    assert set(report.to_dict()) == {"ok", "checks"}
    assert all(local_fix_hint_for_check_name(check.name) for check in report.checks)


def test_subdirectory_resolves_the_actual_repository_for_config_and_probes(repo):
    subdirectory = repo / "src"
    subdirectory.mkdir()
    report = run_local_doctor(subdirectory, which=which, runner=probe_runner([]))
    assert report.ok, report.to_dict()
    assert str(repo) in checks_by_name(report)["repository"].detail


@pytest.mark.parametrize("location", ["parent", "filesystem root"])
def test_local_workshop_ancestor_root_checks_writability_without_legacy_restrictions(
    repo, monkeypatch, location
):
    workshop_root = repo.parent if location == "parent" else Path(repo.anchor)
    if location == "filesystem root":
        real_access = os.access
        monkeypatch.setattr(
            "machinist.doctor.os.access",
            lambda path, mode: (
                True if Path(path) == workshop_root else real_access(path, mode)
            ),
        )
    (repo / "machinist.yaml").write_text(f"workspace:\n  root: {workshop_root}\n")
    git(repo, "add", "machinist.yaml")
    git(repo, "commit", "-qm", "Workshop location")
    before = tree(repo)
    calls = []
    report = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner([]),
        run_gates=True,
        gate_runner=lambda command, **kwargs: (
            calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    assert report.ok, report.to_dict()
    assert checks_by_name(report)["workspace"].level is CheckLevel.PASS
    assert checks_by_name(report)["verification execution"].level is CheckLevel.PASS
    assert calls == ["python -m pytest"]
    assert tree(repo) == before
    assert not (repo / ".machinist").exists()


def test_local_workshop_inside_repository_remains_a_shared_configuration_failure(repo):
    (repo / "machinist.yaml").write_text(f"workspace:\n  root: {repo / 'workshops'}\n")
    git(repo, "add", "machinist.yaml")
    git(repo, "commit", "-qm", "invalid Workshop location")
    before = tree(repo)
    report = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner([]),
        run_gates=True,
        gate_runner=lambda *a, **k: pytest.fail("invalid setup must skip gates"),
    )

    assert not report.ok
    assert checks_by_name(report)["local configuration"].level is CheckLevel.FAIL
    assert "outside the repository" in str(report.to_dict())
    assert tree(repo) == before


def test_pending_runtime_exclusion_warns_about_repository_override_without_writing(
    repo,
):
    (repo / ".gitignore").write_text("!/.machinist/runs/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "explicit runtime unignore")
    before = tree(repo)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))

    check = checks_by_name(report)["runtime exclusion"]
    assert check.level is CheckLevel.WARN
    assert "not established" in check.detail
    assert "start will apply and verify" in check.detail
    assert "repository ignore rules may override" in check.detail
    assert tree(repo) == before
    assert not (repo / ".machinist").exists()


@pytest.mark.parametrize("already_ignored", [False, True])
def test_runtime_exclusion_checks_managed_path_only_when_setup_needs_to_write(
    repo, tmp_path, already_ignored
):
    if already_ignored:
        (repo / ".gitignore").write_text("/.machinist/runs/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "runtime exclusion")
    outside = tmp_path / "personal-exclude"
    outside.write_text("keep me\n")
    exclude = repo / ".git/info/exclude"
    exclude.unlink()
    exclude.symlink_to(outside)
    before = tree(repo)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))

    check = checks_by_name(report)["runtime exclusion"]
    assert check.level is (CheckLevel.PASS if already_ignored else CheckLevel.FAIL)
    assert report.ok is already_ignored
    assert tree(repo) == before
    assert outside.read_text() == "keep me\n"
    assert not (repo / ".machinist").exists()


def test_existing_local_settings_win_over_invalid_root_settings(repo, monkeypatch):
    monkeypatch.setattr("machinist.local_setup.shutil.which", which)
    expected = ensure_local_config(repo, harness_name="codex", test_command="pytest")
    (repo / "machinist.yaml").write_text("invalid: configuration\n")
    git(repo, "add", "machinist.yaml")
    git(repo, "commit", "-qm", "unrelated root settings")
    before = tree(repo)
    gate_calls = []
    report = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner([]),
        run_gates=True,
        gate_runner=lambda command, **kwargs: (
            gate_calls.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    assert report.ok, report.to_dict()
    assert gate_calls == [expected.resolved_verification_gates()[0].command]
    assert "saved" in checks_by_name(report)["local configuration"].detail
    assert tree(repo) == before


@pytest.mark.parametrize("dirty", ["tracked", "untracked", "staged"])
def test_dirty_checkout_reports_failure_without_rewriting_index(repo, dirty):
    target = repo / ("pyproject.toml" if dirty == "tracked" else "new.txt")
    target.write_text(
        target.read_text() + "\n# changed\n" if target.exists() else "new\n"
    )
    if dirty == "staged":
        git(repo, "add", "new.txt")
    before = tree(repo)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(report)["working tree"].level is CheckLevel.FAIL
    assert tree(repo) == before


def test_empty_repository_and_detached_head_have_distinct_fixes(repo):
    git(repo, "checkout", "--detach", "-q")
    detached = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(detached)["local branch"].level is CheckLevel.FAIL
    git(repo, "switch", "--orphan", "empty")
    empty = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(empty)["committed HEAD"].level is CheckLevel.FAIL
    assert checks_by_name(empty)["local branch"].level is CheckLevel.PASS


@pytest.mark.parametrize(
    "field, expected", [("user.name", CheckLevel.PASS), ("user.email", CheckLevel.WARN)]
)
def test_missing_author_configuration_matches_controller_fallback(
    repo, field, expected
):
    git(repo, "config", "--unset", field)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(report)["Git author"].level is expected
    assert "git config" in local_fix_hint_for_check_name("Git author")


def test_configured_email_without_name_matches_git_commit_identity(repo):
    git(repo, "config", "--unset", "user.name")
    before = tree(repo)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert report.ok, report.to_dict()
    assert checks_by_name(report)["Git author"].level is CheckLevel.PASS
    assert tree(repo) == before

    workspace = Workspace(repo, WorkspaceConfig(root=repo.parent / "workshops"))
    (repo / "identity-proof").write_text("Git can derive the name\n")
    workspace.commit_all(repo, "prove effective identity")
    assert workspace._git(repo, "log", "-1", "--format=%an").strip()
    assert (
        workspace._git(repo, "log", "-1", "--format=%ae").strip() == "local@example.com"
    )


def test_explicit_empty_author_name_is_rejected_by_diagnostic_and_git(repo):
    git(repo, "config", "user.name", "")
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(report)["Git author"].level is CheckLevel.FAIL
    failed = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "invalid identity"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert "empty ident name" in failed.stderr


def test_global_identity_only_allows_existing_controller_fallback(
    repo, tmp_path, monkeypatch
):
    git(repo, "config", "--unset", "user.name")
    git(repo, "config", "--unset", "user.email")
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(
        "[user]\nname = Global Developer\nemail = global@example.com\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    before = tree(repo)
    gate_calls = []
    report = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner([]),
        run_gates=True,
        gate_runner=lambda command, **kwargs: (
            gate_calls.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    assert report.ok, report.to_dict()
    assert checks_by_name(report)["Git author"].level is CheckLevel.WARN
    assert "AgentMachinist" in checks_by_name(report)["Git author"].detail
    assert "fallback" in checks_by_name(report)["Git author"].detail
    assert gate_calls == ["python -m pytest"]
    assert tree(repo) == before


def test_missing_gate_is_actionable_and_never_creates_local_config(repo):
    (repo / "pyproject.toml").unlink()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "remove manifest")
    before = tree(repo)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert not report.ok
    assert "--test-cmd" in str(report.to_dict())
    assert tree(repo) == before


def test_missing_harness_fails_before_any_harness_or_forge_probe(repo):
    calls = []
    report = run_local_doctor(
        repo,
        which=lambda name: which(name) if name == "git" else None,
        runner=probe_runner(calls),
    )
    assert not report.ok
    assert "Harness" in str(report.to_dict())
    assert all(args[0] == "git" for args in calls)


def test_unknown_and_partial_phase_harnesses_are_rejected(repo, monkeypatch):
    class SpecOnly(Codex):
        descriptor = replace(Codex.descriptor, phases=frozenset({"spec"}))

    (repo / "machinist.yaml").write_text("harness:\n  name: codex\n")
    monkeypatch.setattr(
        "machinist.local_setup.discover_harnesses",
        lambda: HarnessRegistry({"codex": SpecOnly}),
    )
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert not report.ok
    assert "cannot run execute" in str(report.to_dict())


@pytest.mark.parametrize(
    "relative", ["machinist.yaml", ".machinist", ".machinist/runs/local/config.yaml"]
)
def test_unsafe_configuration_paths_fail_without_touching_targets(
    repo, tmp_path, relative
):
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    if target.suffix:
        outside.write_text("version: 1\n")
    else:
        outside.mkdir()
    target.symlink_to(outside)
    before = tree(outside) if outside.is_dir() else outside.read_bytes()
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert not report.ok
    assert checks_by_name(report)["local configuration"].level is CheckLevel.FAIL
    assert (tree(outside) if outside.is_dir() else outside.read_bytes()) == before


def test_invalid_root_configuration_is_reported_without_traceback(repo):
    (repo / "machinist.yaml").write_text("unknown: invalid\n")
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(report)["local configuration"].level is CheckLevel.FAIL


def test_gate_launchability_is_distinct_from_explicit_execution(repo):
    def available(name):
        return which(name) if name != "python" else None

    report = run_local_doctor(repo, which=available, runner=probe_runner([]))
    assert checks_by_name(report)["verification commands"].level is CheckLevel.FAIL
    assert "not run" in checks_by_name(report)["verification execution"].detail


def test_repository_relative_harness_uses_the_same_lookup_as_start(repo):
    command = repo / "tools/agent"
    command.parent.mkdir()
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: codex\n  command: ./tools/agent\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "local Harness")
    lookup_calls = []

    def locate(name):
        lookup_calls.append(name)
        return str(command) if name == str(command) else which(name)

    def runner(args, **kwargs):
        if args[0] == "./tools/agent":
            assert kwargs["cwd"] == repo
            return subprocess.CompletedProcess(args, 0, "logged in", "")
        return probe_runner([])(args, **kwargs)

    report = run_local_doctor(repo, which=locate, runner=runner)
    assert report.ok, report.to_dict()
    assert "./tools/agent" not in lookup_calls


def test_git_probes_disable_fsmonitor_and_do_not_refresh_index(repo):
    monitor = repo / ".git/fsmonitor.sh"
    marker = repo / "unexpected-monitor-write"
    monitor.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    monitor.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(monitor))
    before = tree(repo)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert report.ok, report.to_dict()
    assert tree(repo) == before
    assert not marker.exists()


def test_git_readiness_timeouts_are_bounded_and_described_accurately(repo):
    def timeout(args, **kwargs):
        assert kwargs["timeout"] == 10
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    report = run_local_doctor(repo, which=which, runner=timeout)
    assert not report.ok
    assert "10 seconds" in checks_by_name(report)["repository"].detail


def test_config_discovery_failure_is_a_report_not_a_traceback(repo, monkeypatch):
    def fail():
        raise RuntimeError("adapter registry unavailable")

    monkeypatch.setattr("machinist.local_setup.discover_harnesses", fail)
    report = run_local_doctor(repo, which=which, runner=probe_runner([]))
    assert checks_by_name(report)["local configuration"].level is CheckLevel.FAIL
    assert "registry unavailable" in str(report.to_dict())


def test_explicit_gates_use_verification_required_advisory_and_mutation_policies(repo):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: codex\nverification:\n  gates:\n    - name: required\n      command: pytest\n    - name: advisory\n      command: pytest --extra\n      required: false\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "gates")
    calls = []

    def gate(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, int("extra" in command), "", "failure"
        )

    report = run_local_doctor(
        repo, which=which, runner=probe_runner([]), run_gates=True, gate_runner=gate
    )
    assert report.ok, report.to_dict()
    assert calls == ["pytest", "pytest --extra"]
    assert checks_by_name(report)["verification execution"].level is CheckLevel.WARN

    failed = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner([]),
        run_gates=True,
        gate_runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "failed"
        ),
    )
    assert checks_by_name(failed)["verification execution"].level is CheckLevel.FAIL


def test_run_gates_does_not_execute_commands_in_a_failed_repository_check(repo):
    (repo / "dirty").write_text("pending work\n")
    report = run_local_doctor(
        repo,
        which=which,
        runner=probe_runner([]),
        run_gates=True,
        gate_runner=lambda *a, **k: pytest.fail("unsafe checkout must block execution"),
    )
    assert checks_by_name(report)["verification execution"].level is CheckLevel.FAIL
    assert "skipped" in checks_by_name(report)["verification execution"].detail


def test_explicit_gate_execution_keeps_the_existing_forbidden_mutation_policy(repo):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: codex\nverification:\n  gates:\n"
        "    - name: immutable\n      command: pytest\n      mutation_policy: forbid\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "gate policy")

    def gate(command, **kwargs):
        (repo / "gate-created.txt").write_text("mutation\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    report = run_local_doctor(
        repo, which=which, runner=probe_runner([]), run_gates=True, gate_runner=gate
    )
    assert not report.ok
    assert (
        "mutation_detected" in checks_by_name(report)["verification execution"].detail
    )
    assert not (repo / ".machinist").exists()


def test_not_a_repository_and_git_lookup_errors_are_reports(tmp_path):
    report = run_local_doctor(tmp_path, which=which, runner=probe_runner([]))
    assert checks_by_name(report)["repository"].level is CheckLevel.FAIL

    def broken_which(name):
        raise OSError("PATH unavailable")

    missing = run_local_doctor(
        tmp_path, which=broken_which, runner=lambda *a, **k: pytest.fail("missing git")
    )
    assert not missing.ok
    assert "PATH unavailable" in str(missing.to_dict())

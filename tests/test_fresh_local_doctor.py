"""Explicit fresh readiness proves committed-checkout Gates without Task state."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from machinist.doctor import CheckLevel
from machinist.local_doctor import local_fix_hint_for_check_name, run_local_doctor
from machinist.workspace import WorkspaceError


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def tree(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def which(command):
    return "/bin/codex" if command == "codex" else shutil.which(command)


def probes(arguments, **kwargs):
    if arguments[0] == "git":
        assert not set(arguments) & {"fetch", "ls-remote", "push", "worktree"}
        return subprocess.run(arguments, **kwargs)
    assert arguments[0] == "codex"
    assert "--version" in arguments or "--help" in arguments or "login" in arguments
    return subprocess.CompletedProcess(arguments, 0, "codex 1.0; logged in", "")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "maintenance.auto", "false")
    git(root, "config", "user.name", "Fresh Readiness Test")
    git(root, "config", "user.email", "readiness@example.com")
    (root / ".gitignore").write_text("/.machinist/runs/\nignored-dependency/\n")
    (root / "README.md").write_text("committed baseline\n")
    (root / "machinist.yaml").write_text(
        "harness:\n  name: codex\n"
        f"workspace:\n  root: {tmp_path / 'configured-workshops'}\n"
        "tests:\n  command: '" + sys.executable + ' -c "pass"\'\n'
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    return root


def checks(report):
    return {check.name: check for check in report.checks}


def test_fresh_workshop_implies_gates_and_preserves_controller_and_configuration(repo):
    before = tree(repo)
    refs = git(repo, "show-ref")
    calls = []

    def gate(command, **kwargs):
        path = Path(kwargs["cwd"])
        calls.append(path)
        assert path != repo
        assert (path / "README.md").read_text() == "committed baseline\n"
        assert git(path, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
        assert git(path, "remote") == ""
        return subprocess.CompletedProcess(command, 0, "", "")

    report = run_local_doctor(
        repo, fresh_workshop=True, which=which, runner=probes, gate_runner=gate
    )

    assert report.ok, report.to_dict()
    assert len(calls) == 1
    assert not calls[0].exists()
    assert checks(report)["fresh Workshop"].level is CheckLevel.PASS
    assert "fresh" in checks(report)["verification execution"].detail.lower()
    assert tree(repo) == before
    assert git(repo, "show-ref") == refs
    assert not (repo / ".machinist").exists()
    assert not (repo.parent / "configured-workshops").exists()
    assert all(local_fix_hint_for_check_name(check.name) for check in report.checks)


def test_fresh_workshop_detects_missing_ignored_dependencies_that_controller_has(repo):
    command = f"{shlex.quote(sys.executable)} -c " + shlex.quote(
        "from pathlib import Path; "
        "assert Path('ignored-dependency/ready').exists(), 'fresh dependency is missing'"
    )
    config = yaml.safe_load((repo / "machinist.yaml").read_text())
    config["tests"]["command"] = command
    (repo / "machinist.yaml").write_text(yaml.safe_dump(config))
    git(repo, "add", "machinist.yaml")
    git(repo, "commit", "-qm", "dependency-sensitive gate")
    dependency = repo / "ignored-dependency/ready"
    dependency.parent.mkdir()
    dependency.write_text("only present in the developer checkout\n")
    before = tree(repo)

    current = run_local_doctor(repo, run_gates=True, which=which, runner=probes)
    fresh = run_local_doctor(repo, fresh_workshop=True, which=which, runner=probes)

    assert current.ok, current.to_dict()
    assert not fresh.ok
    assert checks(fresh)["verification execution"].level is CheckLevel.FAIL
    assert (
        "fresh dependency is missing" in checks(fresh)["verification execution"].detail
    )
    assert tree(repo) == before


def test_fresh_workshop_cleans_failed_gate_and_returns_actionable_structured_check(
    repo,
):
    before = tree(repo)
    paths = []

    def fail(command, **kwargs):
        paths.append(Path(kwargs["cwd"]))
        return subprocess.CompletedProcess(command, 1, "", "missing project dependency")

    report = run_local_doctor(
        repo, fresh_workshop=True, which=which, runner=probes, gate_runner=fail
    )

    assert not report.ok
    assert not paths[0].exists()
    failure = checks(report)["verification execution"]
    assert "missing project dependency" in failure.detail
    assert "--fresh-workshop" in local_fix_hint_for_check_name("fresh Workshop")
    assert any(item["name"] == "fresh Workshop" for item in report.to_dict()["checks"])
    assert tree(repo) == before


def test_fresh_workshop_rejects_git_custody_mutation_and_still_cleans_it(repo):
    before = tree(repo)
    paths = []

    def unauthorized_ref(command, **kwargs):
        path = Path(kwargs["cwd"])
        paths.append(path)
        git(path, "branch", "harness-created")
        return subprocess.CompletedProcess(command, 0, "", "")

    report = run_local_doctor(
        repo,
        fresh_workshop=True,
        which=which,
        runner=probes,
        gate_runner=unauthorized_ref,
    )

    assert not report.ok
    assert "controller-owned refs" in str(report.to_dict())
    assert not paths[0].exists()
    assert tree(repo) == before


@pytest.mark.parametrize("ignored", [True, False])
def test_fresh_readiness_matches_start_baseline_for_dependency_setup_mutations(
    repo, ignored
):
    config = yaml.safe_load((repo / "machinist.yaml").read_text())
    command = config.pop("tests")["command"]
    config["verification"] = {
        "gates": [
            {"name": "dependency setup", "command": command, "mutation_policy": "allow"}
        ]
    }
    (repo / "machinist.yaml").write_text(yaml.safe_dump(config))
    git(repo, "add", "machinist.yaml")
    git(repo, "commit", "-qm", "self-preparing Gate")
    before = tree(repo)
    paths = []

    def prepare(command, **kwargs):
        path = Path(kwargs["cwd"])
        paths.append(path)
        created = path / ("ignored-dependency/ready" if ignored else "generated.lock")
        created.parent.mkdir(exist_ok=True)
        created.write_text("installed by the configured Gate\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    report = run_local_doctor(
        repo, fresh_workshop=True, which=which, runner=probes, gate_runner=prepare
    )
    assert report.ok is ignored, report.to_dict()
    if not ignored:
        assert "changed the Workshop" in checks(report)["fresh Workshop"].detail
    assert not paths[0].exists()
    assert tree(repo) == before


def test_ordinary_readiness_remains_read_only_and_does_not_provision(repo, monkeypatch):
    before = tree(repo)
    monkeypatch.setattr(
        "machinist.local_doctor.LocalWorkspace.provision",
        lambda *args, **kwargs: pytest.fail("ordinary readiness must not provision"),
    )
    report = run_local_doctor(
        repo,
        which=which,
        runner=probes,
        gate_runner=lambda *args, **kwargs: pytest.fail("gates require opt-in"),
    )
    assert report.ok, report.to_dict()
    assert "fresh Workshop" not in checks(report)
    assert tree(repo) == before


def test_failed_preflight_skips_fresh_workshop_and_gates(repo, monkeypatch):
    (repo / "README.md").write_text("uncommitted change\n")
    before = tree(repo)
    monkeypatch.setattr(
        "machinist.local_doctor.LocalWorkspace.provision",
        lambda *args, **kwargs: pytest.fail("failed readiness must not provision"),
    )
    report = run_local_doctor(
        repo,
        fresh_workshop=True,
        which=which,
        runner=probes,
        gate_runner=lambda *args, **kwargs: pytest.fail(
            "failed readiness must skip gates"
        ),
    )
    assert not report.ok
    assert checks(report)["fresh Workshop"].level is CheckLevel.FAIL
    assert "skipped" in checks(report)["fresh Workshop"].detail
    assert tree(repo) == before


def test_fresh_workshop_provisioning_failure_is_diagnosed_without_runtime(
    repo, monkeypatch
):
    before = tree(repo)

    def fail(*args, **kwargs):
        raise WorkspaceError("disposable clone could not be created")

    monkeypatch.setattr("machinist.local_doctor.LocalWorkspace.provision", fail)
    report = run_local_doctor(repo, fresh_workshop=True, which=which, runner=probes)
    assert not report.ok
    assert "could not be created" in checks(report)["fresh Workshop"].detail
    assert tree(repo) == before

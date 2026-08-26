"""Tests for isolated task workspaces, run against real git repos."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from machinist.config import CleanupPolicy, WorkspaceConfig, WorkspaceStrategy
from machinist.workspace import Workspace, WorkspaceError


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A local clone of a bare 'origin' repo, with one commit on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        check=True,
    )
    clone = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)], capture_output=True, check=True
    )
    git(clone, "config", "user.email", "test@example.com")
    git(clone, "config", "user.name", "Test User")
    (clone / "README.md").write_text("hello\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-m", "init")
    git(clone, "push", "-u", "origin", "main")
    return clone


def make_workspace(repo, tmp_path, **overrides):
    config = WorkspaceConfig(root=tmp_path / "ws", **overrides)
    return Workspace(repo_root=repo, config=config)


def test_provision_worktree_creates_branch_from_base(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert path == tmp_path / "ws" / "repo-issue-7"
    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "branch", "--show-current") == "agent/issue-7"


def test_preview_clone_reads_remote_task_head_without_mutating_controller_refs(
    repo, tmp_path
):
    producer = tmp_path / "producer"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(producer)],
        capture_output=True,
        check=True,
    )
    git(producer, "config", "user.email", "producer@example.com")
    git(producer, "config", "user.name", "Producer")
    git(producer, "checkout", "-b", "agent/issue-7", "origin/main")
    (producer / "TASK.md").write_text("remote task head\n")
    git(producer, "add", "TASK.md")
    git(producer, "commit", "-m", "task head")
    git(producer, "push", "origin", "agent/issue-7")
    before = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision_preview(
        "preview-issue-7-deadbeef",
        "agent/issue-7",
        "origin/main",
    )

    assert (path / "TASK.md").read_text() == "remote task head\n"
    assert git(path, "branch", "--show-current") == ""
    assert git(repo, "for-each-ref", "--format=%(refname) %(objectname)") == before

    workspace.cleanup_preview(path)
    assert not path.exists()


def test_preview_cleanup_ignores_retention_policy_and_removes_dirty_clone(
    repo, tmp_path
):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.NEVER)
    path = workspace.provision_preview(
        "preview-issue-7-deadbeef",
        "agent/issue-7",
        "origin/main",
    )
    (path / "untrusted-output.txt").write_text("discard me\n")

    workspace.cleanup_preview(path)

    assert not path.exists()


def test_preview_cleanup_refuses_unclaimed_prefix_collision(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    victim = tmp_path / "ws" / f"{repo.name}-preview-personal-notes"
    victim.mkdir(parents=True)
    evidence = victim / "do-not-delete.txt"
    evidence.write_text("unrelated data\n")

    with pytest.raises(WorkspaceError, match="no live controller ownership claim"):
        workspace.cleanup_preview(victim)

    assert evidence.read_text() == "unrelated data\n"


def test_preview_cleanup_refuses_replaced_claimed_directory(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision_preview(
        "preview-issue-7-deadbeef",
        "agent/issue-7",
        "origin/main",
    )
    original = path.with_name(path.name + "-original")
    path.rename(original)
    path.mkdir()
    evidence = path / "do-not-delete.txt"
    evidence.write_text("replacement data\n")

    with pytest.raises(WorkspaceError, match="no longer matches its ownership claim"):
        workspace.cleanup_preview(path)

    assert evidence.read_text() == "replacement data\n"


def test_provision_reuses_existing_branch(repo, tmp_path):
    git(repo, "branch", "agent/issue-7", "origin/main")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert git(path, "branch", "--show-current") == "agent/issue-7"


def test_provision_fails_cleanly_when_path_exists(repo, tmp_path):
    (tmp_path / "ws" / "repo-issue-7").mkdir(parents=True)
    workspace = make_workspace(repo, tmp_path)

    with pytest.raises(WorkspaceError, match="repo-issue-7"):
        workspace.provision("issue-7", "agent/issue-7", "origin/main")


def test_provision_rejects_task_names_that_escape_workspace_root(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)

    with pytest.raises(WorkspaceError, match="task name"):
        workspace.provision("../../outside", "agent/issue-7", "origin/main")

    assert not (tmp_path / "outside").exists()


def test_commit_all_commits_new_files(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "spec.md").write_text("the spec\n")

    workspace.commit_all(path, "docs(spec): add spec")

    assert git(path, "log", "-1", "--format=%s") == "docs(spec): add spec"
    assert git(path, "status", "--porcelain") == ""


def test_commit_all_falls_back_to_bot_identity(repo, tmp_path, monkeypatch):
    # Hide global/system git config and the repo's local identity so the
    # environment looks like a bare CI runner.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    git(repo, "config", "--unset", "user.email")
    git(repo, "config", "--unset", "user.name")
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "spec.md").write_text("the spec\n")

    workspace.commit_all(path, "docs(spec): add spec")

    assert "AgentMachinist" in git(path, "log", "-1", "--format=%an")


def test_push_publishes_branch_to_origin(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "spec.md").write_text("the spec\n")
    workspace.commit_all(path, "docs(spec): add spec")

    workspace.push(path, "agent/issue-7")

    origin = tmp_path / "origin.git"
    heads = git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert "agent/issue-7" in heads.splitlines()


def test_actions_push_credential_is_ephemeral_git_process_config(
    repo, tmp_path, monkeypatch
):
    workspace = make_workspace(repo, tmp_path)
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-cloud-secret")
    monkeypatch.setenv("DATABASE_PASSWORD", "unrelated-database-secret")

    environment = workspace._ephemeral_push_environment()

    assert environment is not None
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    expected = base64.b64encode(b"x-access-token:test-token").decode()
    assert environment["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert "test-token" not in environment["GIT_CONFIG_VALUE_0"]
    assert "GH_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "DATABASE_PASSWORD" not in environment


def test_private_github_auth_is_bounded_to_network_git_children(
    repo, tmp_path, monkeypatch
):
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    workspace = Workspace(
        repo_root=repo,
        config=WorkspaceConfig(root=tmp_path / "ws"),
        runner=runner,
    )
    monkeypatch.setenv("GH_TOKEN", "private-repo-token")
    origin = "https://github.com/example/private.git"
    network_env = workspace._ephemeral_network_environment(origin)
    assert network_env is not None

    workspace._git(repo, "fetch", origin, env=network_env)
    workspace._git(repo, "ls-remote", origin, env=network_env)
    workspace._git(repo, "status")

    for _command, kwargs in calls[:2]:
        environment = kwargs["env"]
        assert environment["GIT_CONFIG_COUNT"] == "1"
        assert environment["GIT_CONFIG_KEY_0"] == (
            "http.https://github.com/.extraheader"
        )
        assert "GH_TOKEN" not in environment
        assert "private-repo-token" not in environment.values()
    local_environment = calls[2][1]["env"]
    assert "GIT_CONFIG_COUNT" not in local_environment
    assert "GIT_CONFIG_VALUE_0" not in local_environment


def test_authenticated_gh_supplies_private_network_auth_without_exported_token(
    repo, tmp_path, monkeypatch
):
    git_calls = []
    auth_calls = []

    def git_runner(args, **kwargs):
        git_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    def auth_runner(args, **kwargs):
        auth_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "keychain-backed-token\n", "")

    monkeypatch.delenv("GH_TOKEN", raising=False)
    workspace = Workspace(
        repo_root=repo,
        config=WorkspaceConfig(root=tmp_path / "ws"),
        runner=git_runner,
        auth_runner=auth_runner,
    )
    origin = "https://github.com/example/private.git"

    for operation in ("fetch", "ls-remote", "push"):
        network_env = workspace._ephemeral_network_environment(origin)
        assert network_env is not None
        workspace._git(repo, operation, origin, env=network_env)

    assert len(auth_calls) == 1
    auth_command, auth_kwargs = auth_calls[0]
    assert auth_command == ["gh", "auth", "token", "--hostname", "github.com"]
    assert auth_kwargs["timeout"] == 10
    assert auth_kwargs["capture_output"] is True
    assert auth_kwargs["text"] is True
    assert auth_kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
    assert "GH_TOKEN" not in auth_kwargs["env"]
    for _command, kwargs in git_calls:
        environment = kwargs["env"]
        assert environment["GIT_CONFIG_KEY_0"] == (
            "http.https://github.com/.extraheader"
        )
        assert "keychain-backed-token" not in environment.values()
        assert "GH_TOKEN" not in environment


def test_public_github_network_flow_remains_credential_free_without_gh_auth(
    repo, tmp_path, monkeypatch
):
    git_calls = []
    auth_calls = []

    def git_runner(args, **kwargs):
        git_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    def unauthenticated_gh(args, **kwargs):
        auth_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "", "not logged in")

    monkeypatch.delenv("GH_TOKEN", raising=False)
    workspace = Workspace(
        repo_root=repo,
        config=WorkspaceConfig(root=tmp_path / "ws"),
        runner=git_runner,
        auth_runner=unauthenticated_gh,
    )
    origin = "https://github.com/example/public.git"

    assert workspace._ephemeral_network_environment(origin) is None
    workspace._git(repo, "ls-remote", origin)

    assert auth_calls[0][0] == [
        "gh",
        "auth",
        "token",
        "--hostname",
        "github.com",
    ]
    environment = git_calls[0][1]["env"]
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_VALUE_0" not in environment
    assert "GH_TOKEN" not in environment


def test_dotcom_token_is_never_forwarded_to_ambient_enterprise_host(
    repo, tmp_path, monkeypatch
):
    auth_calls = []

    def unauthenticated_gh(args, **kwargs):
        auth_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "", "not logged in")

    monkeypatch.setenv("GH_HOST", "ghe.attacker.test")
    monkeypatch.setenv("GH_TOKEN", "github-dot-com-secret")
    monkeypatch.delenv("GH_ENTERPRISE_TOKEN", raising=False)
    workspace = Workspace(
        repo_root=repo,
        config=WorkspaceConfig(root=tmp_path / "ws"),
        auth_runner=unauthenticated_gh,
    )

    environment = workspace._ephemeral_network_environment(
        "https://ghe.attacker.test/owner/repo.git"
    )

    assert environment is None
    assert auth_calls[0][0] == [
        "gh",
        "auth",
        "token",
        "--hostname",
        "ghe.attacker.test",
    ]
    assert "GH_TOKEN" not in auth_calls[0][1]["env"]
    assert "github-dot-com-secret" not in auth_calls[0][1]["env"].values()


def test_enterprise_token_is_bounded_to_its_exact_configured_host(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "ghe.example.test:8443")
    monkeypatch.setenv("GH_TOKEN", "github-dot-com-secret")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "enterprise-secret")
    workspace = make_workspace(repo, tmp_path)

    environment = workspace._ephemeral_network_environment(
        "https://ghe.example.test:8443/owner/repo.git"
    )

    assert environment is not None
    encoded = environment["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(encoded).decode() == "x-access-token:enterprise-secret"
    assert "github-dot-com-secret" not in environment.values()


def test_custody_evidence_hashes_and_redacts_credentialed_origin(repo, tmp_path):
    credentialed = (
        "https://build-user:super-secret-token@github.com/example/private.git"
    )
    git(repo, "remote", "set-url", "origin", credentialed)
    workspace = make_workspace(repo, tmp_path)

    token = workspace.capture_git_custody(repo)
    serialized = json.dumps(token, sort_keys=True)

    assert (
        token["origin_identity_sha256"]
        == hashlib.sha256(credentialed.encode()).hexdigest()
    )
    assert token["origin_display"] == "https://github.com/example/private.git"
    assert "super-secret-token" not in serialized
    assert "build-user" not in serialized
    assert "origin_url" not in token

    resumed = make_workspace(repo, tmp_path)
    resumed.assert_git_custody(repo, token)


@pytest.mark.parametrize(
    "origin",
    [
        "https://build-user:secret@github.com/VSCarpenter/AgentMachinist.git",
        "ssh://git@github.com/VSCarpenter/AgentMachinist.git",
        "git@github.com:VSCarpenter/AgentMachinist.git",
    ],
)
def test_repository_identity_accepts_github_https_and_ssh_forms(repo, tmp_path, origin):
    git(repo, "remote", "set-url", "origin", origin)
    workspace = make_workspace(repo, tmp_path)

    assert workspace.repository_identity() == "vscarpenter/agentmachinist"
    assert workspace.repository_target() == (
        "github.com",
        "vscarpenter/agentmachinist",
    )


def test_repository_target_binds_enterprise_host_and_nondefault_port(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "GHE.Example.Test:8443")
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://ghe.example.test:8443/Owner/Repo.git",
    )
    workspace = make_workspace(repo, tmp_path)

    assert workspace.repository_target() == (
        "ghe.example.test:8443",
        "owner/repo",
    )


def test_repository_target_dotcom_ignores_hostile_ambient_gh_host(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "ghe.attacker.test")
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://github.com/Owner/Repo.git",
    )

    assert make_workspace(repo, tmp_path).repository_target() == (
        "github.com",
        "owner/repo",
    )


@pytest.mark.parametrize(
    "malformed_host",
    ["ghe.example:abc", "https://ghe.example:abc", "ghe.example:99999"],
)
def test_repository_target_ignores_malformed_ambient_gh_host(
    repo, tmp_path, monkeypatch, malformed_host
):
    monkeypatch.setenv("GH_HOST", malformed_host)
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://github.com/Owner/Repo.git",
    )

    assert make_workspace(repo, tmp_path).repository_target() == (
        "github.com",
        "owner/repo",
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com:abc/owner/repo.git",
        "https://github.com:99999/owner/repo.git",
    ],
)
def test_repository_target_reports_malformed_origin_port_as_workspace_error(
    repo, tmp_path, origin
):
    git(repo, "remote", "set-url", "origin", origin)

    with pytest.raises(WorkspaceError, match="not a recognized GitHub"):
        make_workspace(repo, tmp_path).repository_target()


def test_custody_capture_redacts_malformed_origin_port_without_raw_value_error(
    repo, tmp_path
):
    malformed = "https://github.com:abc/owner/repo.git"
    git(repo, "remote", "set-url", "origin", malformed)

    token = make_workspace(repo, tmp_path).capture_git_custody(repo)

    assert token["origin_display"] == "<redacted origin>"
    assert (
        token["origin_identity_sha256"]
        == hashlib.sha256(malformed.encode()).hexdigest()
    )


def test_repository_target_rejects_unconfigured_enterprise_port(
    repo, tmp_path, monkeypatch
):
    monkeypatch.setenv("GH_HOST", "ghe.example.test:8443")
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://ghe.example.test:9443/Owner/Repo.git",
    )

    with pytest.raises(WorkspaceError, match="not a recognized GitHub"):
        make_workspace(repo, tmp_path).repository_target()


def test_repository_identity_fails_closed_for_non_github_origin(repo, tmp_path):
    git(repo, "remote", "set-url", "origin", "https://gitlab.com/owner/repo.git")
    workspace = make_workspace(repo, tmp_path)

    with pytest.raises(WorkspaceError, match="not a recognized GitHub"):
        workspace.repository_identity()


def test_controller_git_disables_planted_hooks_and_does_not_leak_secrets(
    repo, tmp_path, monkeypatch
):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    common_dir = Path(git(path, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    leak = tmp_path / "hook-leak.txt"
    hook = common_dir / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf '%s' \"$GH_TOKEN\" > {leak}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GH_TOKEN", "controller-secret")
    (path / "implementation.md").write_text("implementation\n")

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.commit_all(path, "implementation")

    assert not leak.exists()
    assert git(path, "rev-parse", "HEAD") == git(repo, "rev-parse", "origin/main")


def test_controller_git_disables_preexisting_repository_hook(
    repo, tmp_path, monkeypatch
):
    common_dir = Path(git(repo, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (repo / common_dir).resolve()
    leak = tmp_path / "preexisting-hook-leak.txt"
    hook = common_dir / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf '%s' \"$GH_TOKEN\" > {leak}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GH_TOKEN", "controller-secret")
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "implementation.md").write_text("implementation\n")

    workspace.commit_all(path, "implementation")

    assert not leak.exists()
    assert git(path, "log", "-1", "--format=%s") == "implementation"


@pytest.mark.parametrize(
    "marker",
    ["agentmachinist-start-sha", "agentmachinist-target-branch"],
)
def test_git_custody_detects_controller_marker_tampering(repo, tmp_path, marker):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    git_dir = Path(git(path, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = (path / git_dir).resolve()
    (git_dir / marker).write_text("attacker-controlled\n")

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.assert_git_custody(path)


def test_git_custody_rejects_sparse_oversized_hook_before_reading_or_running_git(
    repo, tmp_path, monkeypatch
):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    common_dir = Path(git(path, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (path / common_dir).resolve()
    hook = common_dir / "hooks" / "post-checkout"
    hook.touch()
    os.truncate(hook, 1 << 40)

    def reject_git(*_args, **_kwargs):
        pytest.fail("custody rejection must happen before a Git subprocess")

    monkeypatch.setattr(workspace, "_git", reject_git)

    with pytest.raises(WorkspaceError, match="per-file custody limit"):
        workspace.assert_git_custody(path)


def test_push_refuses_origin_redirection_and_never_contacts_attacker(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "implementation.md").write_text("implementation\n")
    workspace.commit_all(path, "implementation")
    attacker = tmp_path / "attacker.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(attacker)],
        capture_output=True,
        check=True,
    )
    git(path, "remote", "set-url", "origin", str(attacker))

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.push(path, "agent/issue-7")

    attacker_heads = git(attacker, "for-each-ref", "--format=%(refname)", "refs/heads")
    real_heads = git(
        tmp_path / "origin.git",
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/agent/issue-7",
    )
    assert attacker_heads == ""
    assert real_heads == ""


def test_raw_custody_check_blocks_replaced_git_pointer_before_fsmonitor_runs(
    repo, tmp_path, monkeypatch
):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    attacker = tmp_path / "attacker-repo"
    subprocess.run(
        ["git", "init", "-b", "main", str(attacker)],
        capture_output=True,
        check=True,
    )
    leak = tmp_path / "fsmonitor-leak.txt"
    monitor = tmp_path / "malicious-fsmonitor.sh"
    monitor.write_text(f"#!/bin/sh\nprintf '%s' \"$GH_TOKEN\" > {leak}\n")
    monitor.chmod(0o755)
    git(attacker, "config", "core.fsmonitor", str(monitor))
    monkeypatch.setenv("GH_TOKEN", "controller-secret")
    (path / ".git").write_text(f"gitdir: {attacker / '.git'}\n")

    with pytest.raises(WorkspaceError, match="Git directory identity changed"):
        workspace.has_changes(path)

    assert not leak.exists()


def test_raw_custody_check_blocks_planted_clean_filter_before_git_add(
    repo, tmp_path, monkeypatch
):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    leak = tmp_path / "filter-leak.txt"
    filter_script = tmp_path / "malicious-filter.sh"
    filter_script.write_text(f"#!/bin/sh\nprintf '%s' \"$GH_TOKEN\" > {leak}\ncat\n")
    filter_script.chmod(0o755)
    monkeypatch.setenv("GH_TOKEN", "controller-secret")
    git(path, "config", "filter.exfil.clean", str(filter_script))
    (path / ".gitattributes").write_text("*.md filter=exfil\n")
    (path / "implementation.md").write_text("implementation\n")

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.commit_all(path, "implementation")

    assert not leak.exists()


def test_push_with_lease_refuses_when_remote_changed(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    expected = git(path, "rev-parse", "HEAD")
    (path / "impl.md").write_text("implementation\n")
    workspace.commit_all(path, "implementation")

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)], check=True
    )
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "-b", "agent/issue-7", "origin/main")
    (other / "spec.md").write_text("changed spec\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "change spec")
    git(other, "push", "origin", "agent/issue-7")

    with pytest.raises(WorkspaceError, match="push"):
        workspace.push(path, "agent/issue-7", expected_sha=expected)


def test_push_publishes_detached_fresh_attempt_head_not_stale_local_branch(
    repo, tmp_path
):
    workspace = make_workspace(repo, tmp_path)
    first = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (first / "spec.md").write_text("spec\n")
    workspace.commit_all(first, "spec")
    workspace.push(first, "agent/issue-7")
    remote_spec_sha = workspace.remote_sha(first, "agent/issue-7")

    fresh = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)
    assert git(fresh, "branch", "--show-current") == ""
    assert workspace.head_sha(fresh) == remote_spec_sha
    (fresh / "implementation.md").write_text("implementation\n")
    workspace.commit_all(fresh, "implementation")
    implementation_sha = workspace.head_sha(fresh)

    workspace.push(fresh, "agent/issue-7", expected_sha=remote_spec_sha)

    assert workspace.remote_sha(fresh, "agent/issue-7") == implementation_sha


def test_head_sha_returns_current_commit(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    assert workspace.head_sha(path) == git(path, "rev-parse", "HEAD")


def test_branch_head_and_changed_file_helpers(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    expected = workspace.head_sha(path)

    assert workspace.current_branch(path) == "agent/issue-7"
    workspace.assert_branch(path, "agent/issue-7")
    workspace.assert_head(path, expected)
    assert workspace.changed_files(path) == []

    (path / "README.md").write_text("changed\n")
    (path / "new.txt").write_text("new\n")
    assert workspace.changed_files(path) == ["README.md", "new.txt"]

    with pytest.raises(WorkspaceError, match="expected HEAD"):
        workspace.assert_head(path, "f" * 40)

    with pytest.raises(WorkspaceError, match="expected branch"):
        workspace.assert_branch(path, "agent/issue-8")


def test_change_snapshot_detects_status_and_content_mutations(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    clean = workspace.change_snapshot(path)
    assert workspace.change_snapshot(path) == clean

    new_file = path / "new.txt"
    new_file.write_text("one\n")
    untracked = workspace.change_snapshot(path)
    assert untracked != clean
    assert workspace.change_snapshot(path) == untracked

    new_file.write_text("two\n")
    different_content = workspace.change_snapshot(path)
    assert different_content not in {clean, untracked}

    git(path, "add", "new.txt")
    staged = workspace.change_snapshot(path)
    assert staged not in {clean, untracked, different_content}

    new_file.write_text("three\n")
    staged_and_unstaged = workspace.change_snapshot(path)
    assert staged_and_unstaged not in {
        clean,
        untracked,
        different_content,
        staged,
    }


def test_cleanup_on_success_removes_worktree(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.ON_SUCCESS)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=True)

    assert not path.exists()
    assert str(path) not in git(repo, "worktree", "list")


def test_cleanup_on_success_keeps_failed_workspace_for_debugging(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.ON_SUCCESS)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=False)

    assert path.exists()


def test_cleanup_policy_never_keeps_workspace(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.NEVER)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=True)

    assert path.exists()


def test_cleanup_policy_always_removes_even_on_failure(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, cleanup=CleanupPolicy.ALWAYS)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    workspace.cleanup(path, success=False)

    assert not path.exists()


def test_non_force_remove_preserves_dirty_worktree(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    evidence = path / "diagnostic.txt"
    evidence.write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="uncommitted changes|worktree remove"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert evidence.read_text() == "keep me\n"

    workspace.remove_workspace(path, force=True)
    assert not path.exists()


def test_non_force_remove_preserves_dirty_clone(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    evidence = path / "diagnostic.txt"
    evidence.write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="uncommitted changes"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert evidence.read_text() == "keep me\n"

    workspace.remove_workspace(path, force=True)
    assert not path.exists()


def test_non_force_remove_preserves_unpushed_clone_commit(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (path / "diagnostic.txt").write_text("committed evidence\n")
    workspace.commit_all(path, "unpublished diagnostic commit")

    with pytest.raises(WorkspaceError, match="unpushed commits"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert (path / "diagnostic.txt").exists()


def test_non_force_remove_preserves_unpushed_detached_attempt(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)
    (path / "diagnostic.txt").write_text("committed evidence\n")
    workspace.commit_all(path, "unpublished diagnostic commit")

    with pytest.raises(WorkspaceError, match="unpushed commits"):
        workspace.remove_workspace(path)

    assert path.exists()
    assert (path / "diagnostic.txt").exists()


def test_remove_rejects_path_outside_managed_root_even_with_force(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evidence.txt").write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="outside managed workspace root"):
        workspace.remove_workspace(outside, force=True)

    assert (outside / "evidence.txt").read_text() == "keep me\n"


def test_remove_rejects_managed_symlink_alias_even_with_force(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    target = root / "repo-issue-8"
    target.mkdir()
    evidence = target / "evidence.txt"
    evidence.write_text("keep me\n")
    alias = root / "repo-issue-7"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceError, match="symbolic link"):
        workspace.remove_workspace(alias, force=True)

    assert alias.is_symlink()
    assert evidence.read_text() == "keep me\n"


def test_provision_from_remote_only_branch(repo, tmp_path):
    # Simulate another machine having pushed the spec branch: it exists on
    # origin (one commit ahead of main) but not locally.
    git(repo, "checkout", "-q", "-b", "agent/issue-7", "origin/main")
    (repo / "extra.md").write_text("from spec phase\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "spec")
    git(repo, "push", "origin", "agent/issue-7")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-D", "agent/issue-7")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert git(path, "branch", "--show-current") == "agent/issue-7"
    assert (path / "extra.md").exists()


def test_provision_discovers_remote_branch_from_single_branch_clone(repo, tmp_path):
    git(repo, "checkout", "-q", "-b", "agent/issue-7", "origin/main")
    (repo / "spec.md").write_text("remote spec\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "spec")
    git(repo, "push", "origin", "agent/issue-7")
    remote_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")

    narrow = tmp_path / "narrow"
    subprocess.run(
        [
            "git",
            "clone",
            "--single-branch",
            "--branch",
            "main",
            str(tmp_path / "origin.git"),
            str(narrow),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    git(narrow, "config", "user.email", "test@example.com")
    git(narrow, "config", "user.name", "Test User")
    assert (
        subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "refs/remotes/origin/agent/issue-7",
            ],
            cwd=narrow,
            capture_output=True,
        ).returncode
        != 0
    )
    workspace = Workspace(
        repo_root=narrow,
        config=WorkspaceConfig(root=tmp_path / "narrow-ws"),
    )

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.head_sha(path) == remote_sha
    assert (path / "spec.md").read_text() == "remote spec\n"


def test_provision_fast_forwards_stale_local_branch(repo, tmp_path):
    # Local branch exists at origin/main, but origin's copy is one commit
    # ahead (e.g. the spec was edited on GitHub). Provision must land on
    # the origin tip, not the stale local one.
    git(repo, "branch", "agent/issue-7", "origin/main")
    git(repo, "checkout", "-q", "agent/issue-7")
    (repo / "spec-edit.md").write_text("edited on GitHub\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "spec edit")
    git(repo, "push", "origin", "agent/issue-7")
    git(repo, "reset", "--hard", "-q", "origin/main")
    git(repo, "checkout", "-q", "main")
    workspace = make_workspace(repo, tmp_path)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert (path / "spec-edit.md").exists()


def test_provision_discards_rejected_local_commit_before_retry(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    first = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (first / "spec.md").write_text("approved spec\n")
    workspace.commit_all(first, "approved spec")
    workspace.push(first, "agent/issue-7")
    approved_sha = workspace.remote_sha(first, "agent/issue-7")

    # Simulate a Harness violating Git custody. The rejected commit remains on
    # the local Task branch after the failed Workshop is force-cleaned.
    (first / "rejected.txt").write_text("must never be published\n")
    workspace.commit_all(first, "harness-created rejected commit")
    rejected_sha = workspace.head_sha(first)
    assert rejected_sha != approved_sha
    workspace.remove_workspace(first, force=True)
    assert git(repo, "rev-parse", "agent/issue-7") == rejected_sha

    retried = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.head_sha(retried) == approved_sha
    assert not (retried / "rejected.txt").exists()
    assert git(repo, "rev-parse", "agent/issue-7") == approved_sha


def test_provision_uses_remote_when_local_task_branch_has_diverged(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    first = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (first / "local-only.txt").write_text("rejected\n")
    workspace.commit_all(first, "rejected local commit")
    workspace.remove_workspace(first, force=True)

    other = tmp_path / "other-diverged"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(other)],
        capture_output=True,
        check=True,
    )
    git(other, "config", "user.email", "other@example.com")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "-b", "agent/issue-7", "origin/main")
    (other / "remote-only.txt").write_text("approved remote\n")
    git(other, "add", "-A")
    git(other, "commit", "-m", "remote spec")
    git(other, "push", "origin", "agent/issue-7")
    remote_sha = git(other, "rev-parse", "HEAD")

    retried = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.head_sha(retried) == remote_sha
    assert (retried / "remote-only.txt").exists()
    assert not (retried / "local-only.txt").exists()


def test_has_changes_reflects_working_tree(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert workspace.has_changes(path) is False
    (path / "new.md").write_text("hi\n")
    assert workspace.has_changes(path) is True


def test_clone_strategy_provisions_independent_clone(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path, strategy=WorkspaceStrategy.CLONE)

    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")

    assert (path / "README.md").read_text() == "hello\n"
    assert git(path, "branch", "--show-current") == "agent/issue-7"
    # The clone's origin is the real origin, so push targets the same remote.
    assert git(path, "remote", "get-url", "origin") == str(tmp_path / "origin.git")


def test_explicit_attempt_path_starts_fresh_while_failed_worktree_is_retained(
    repo, tmp_path
):
    workspace = make_workspace(repo, tmp_path)
    retained = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    (retained / "failed-change.txt").write_text("diagnostic evidence\n")
    expected_sha = workspace.head_sha(retained)

    fresh = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)

    assert retained.exists()
    assert (retained / "failed-change.txt").exists()
    assert fresh == tmp_path / "ws" / "repo-issue-7-attempt-2"
    assert workspace.head_sha(fresh) == expected_sha
    assert not (fresh / "failed-change.txt").exists()


def test_resume_validates_managed_checkout_branch_and_head(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main", attempt=2)
    expected_sha = workspace.head_sha(path)
    (path / "diagnostic.txt").write_text("unfinished work\n")

    resumed = workspace.resume(path, branch="agent/issue-7", expected_sha=expected_sha)

    assert resumed == path.resolve()
    assert (resumed / "diagnostic.txt").exists()

    with pytest.raises(WorkspaceError, match="expected HEAD"):
        workspace.resume(path, branch="agent/issue-7", expected_sha="f" * 40)

    with pytest.raises(WorkspaceError, match="expected branch"):
        workspace.resume(path, branch="agent/issue-8", expected_sha=expected_sha)


def test_workspace_management_helpers(repo, tmp_path):
    workspace = make_workspace(repo, tmp_path)
    # workspace_for_task
    ws_path = workspace.workspace_for_task("issue-7")
    assert ws_path == tmp_path / "ws" / f"{repo.name}-issue-7"

    # Initially empty list_workspaces
    assert workspace.list_workspaces() == []

    # Provision a worktree
    path = workspace.provision("issue-7", "agent/issue-7", "origin/main")
    assert path.exists()

    workspaces = workspace.list_workspaces()
    assert len(workspaces) == 1
    assert workspaces[0] == path

    # remove_workspace
    workspace.remove_workspace(path)
    assert not path.exists()
    assert workspace.list_workspaces() == []


def test_workspace_listing_and_force_clean_ignore_unmarked_prefix_collision(
    repo, tmp_path
):
    workspace = make_workspace(repo, tmp_path)
    victim = tmp_path / "ws" / f"{repo.name}-personal-notes"
    victim.mkdir(parents=True)
    evidence = victim / "do-not-delete.txt"
    evidence.write_text("unrelated data\n")

    assert workspace.list_workspaces() == []
    with pytest.raises(WorkspaceError, match="ownership marker"):
        workspace.remove_workspace(victim, force=True)

    assert evidence.read_text() == "unrelated data\n"


def test_same_basename_repository_cannot_list_or_remove_other_owner_workspace(
    repo, tmp_path
):
    shared_root = tmp_path / "shared-workspaces"
    first = Workspace(repo, WorkspaceConfig(root=shared_root))
    owned = first.provision("issue-7", "agent/issue-7", "origin/main")

    second_parent = tmp_path / "second"
    second_parent.mkdir()
    second_repo = second_parent / repo.name
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(second_repo)],
        capture_output=True,
        check=True,
    )
    second = Workspace(second_repo, WorkspaceConfig(root=shared_root))

    assert second.list_workspaces() == []
    with pytest.raises(WorkspaceError, match="ownership marker"):
        second.remove_workspace(owned, force=True)

    assert owned.exists()
    first.remove_workspace(owned, force=True)
    assert not owned.exists()


def test_markerless_exact_task_workspace_is_not_implicitly_adopted_or_deleted(
    repo, tmp_path
):
    original = make_workspace(repo, tmp_path)
    path = original.provision("issue-7", "agent/issue-7", "origin/main")
    marker = Path(git(path, "rev-parse", "--git-path", "agentmachinist-owner.json"))
    if not marker.is_absolute():
        marker = (path / marker).resolve()
    marker.unlink()
    resumed = make_workspace(repo, tmp_path)

    assert resumed.list_workspaces() == []
    assert resumed.list_task_workspaces("issue-7") == []
    with pytest.raises(WorkspaceError, match="ownership marker"):
        resumed.remove_workspace(path, force=True)

    assert path.exists()


def test_git_timeout_becomes_typed_workspace_error(repo, tmp_path):
    def timeout_runner(args, **kwargs):
        assert kwargs["timeout"] == 300
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    workspace = Workspace(
        repo_root=repo,
        config=WorkspaceConfig(root=tmp_path / "ws"),
        runner=timeout_runner,
    )

    with pytest.raises(WorkspaceError, match="timed out"):
        workspace.head_sha(repo)


# --- Issue #16: a worktree shares .git/config and .git/hooks with its parent ---


@pytest.fixture
def worktree_custody(repo, tmp_path):
    """A provisioned worktree plus a fresh custody token for it."""
    workspace = make_workspace(repo, tmp_path)
    path = workspace.provision("issue-16", "agent/issue-16", "origin/main")
    return workspace, path, workspace.capture_git_custody(path)


def test_custody_tolerates_benign_edits_to_the_shared_parent_config(
    worktree_custody, tmp_path
):
    workspace, path, token = worktree_custody
    repo_root = workspace.repo_root

    git(repo_root, "config", "--local", "diff.tool", "vimdiff")
    git(repo_root, "config", "--local", "user.name", "Someone Else")
    git(repo_root, "remote", "add", "upstream", str(tmp_path / "origin.git"))

    workspace.assert_git_custody(path, token)


def test_custody_blocks_a_planted_fsmonitor_in_the_shared_parent_config(
    worktree_custody,
):
    workspace, path, token = worktree_custody

    git(workspace.repo_root, "config", "--local", "core.fsmonitor", "/tmp/evil")

    with pytest.raises(WorkspaceError, match="core.fsmonitor"):
        workspace.assert_git_custody(path, token)


def test_custody_blocks_an_added_include_in_the_shared_parent_config(worktree_custody):
    workspace, path, token = worktree_custody

    git(workspace.repo_root, "config", "--local", "include.path", "/tmp/evil.cfg")

    with pytest.raises(WorkspaceError, match="include.path"):
        workspace.assert_git_custody(path, token)


def test_custody_rejection_points_at_the_clone_strategy_remedy(worktree_custody):
    workspace, path, token = worktree_custody

    git(workspace.repo_root, "config", "--local", "core.pager", "/tmp/evil")

    with pytest.raises(WorkspaceError) as excinfo:
        workspace.assert_git_custody(path, token)
    message = str(excinfo.value)
    assert "worktree" in message
    assert "workspace.strategy: clone" in message


def test_custody_still_byte_checks_shared_hooks(worktree_custody):
    """Hook bodies are code, so they stay strictly compared."""
    workspace, path, token = worktree_custody
    hook = workspace.repo_root / ".git" / "hooks" / "pre-commit"

    hook.write_text("#!/bin/sh\nexit 0\n")

    with pytest.raises(WorkspaceError, match="Git metadata changed"):
        workspace.assert_git_custody(path, token)


def test_custody_falls_back_to_byte_comparison_for_unparsable_config(worktree_custody):
    workspace, path, token = worktree_custody
    config = workspace.repo_root / ".git" / "config"

    config.write_text(config.read_text() + "[core.deprecated]\n\tkey = value\n")

    with pytest.raises(WorkspaceError, match="could not be read as Git config"):
        workspace.assert_git_custody(path, token)

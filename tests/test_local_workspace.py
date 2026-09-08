"""Local Git custody and integration contracts against real repositories."""

import base64
import subprocess
from pathlib import Path

import pytest

from machinist.config import WorkspaceConfig, WorkspaceStrategy
from machinist.local_workspace import LocalWorkspace
from machinist.workspace import WorkspaceError


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def local_repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Local Developer")
    git(path, "config", "user.email", "local@example.com")
    (path / "README.md").write_text("baseline\n")
    (path / ".gitignore").write_text("/.machinist/runs/\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "baseline")
    return path


def workshop(local_repo, tmp_path, strategy=WorkspaceStrategy.WORKTREE):
    return LocalWorkspace(
        local_repo, WorkspaceConfig(root=tmp_path / "workshops", strategy=strategy)
    )


@pytest.mark.parametrize("strategy", list(WorkspaceStrategy))
def test_provision_uses_exact_local_commit_without_origin_or_controller_ref_changes(
    local_repo, tmp_path, strategy
):
    source = workshop(local_repo, tmp_path, strategy)
    base = git(local_repo, "rev-parse", "HEAD")
    refs = git(local_repo, "show-ref")

    path = source.provision("task-1", "agent/task-1", base)

    assert source.head_sha(path) == base
    assert git(path, "branch", "--show-current") == ""
    assert git(path, "remote") == ""
    assert git(local_repo, "show-ref") == refs
    assert source.git_custody(path)["repository_mode"] == "local"


@pytest.mark.parametrize("strategy", list(WorkspaceStrategy))
def test_commit_retains_candidate_with_cas_and_survives_workshop_cleanup(
    local_repo, tmp_path, strategy
):
    source = workshop(local_repo, tmp_path, strategy)
    base = source.resolve_commit("HEAD")
    path = source.provision("task-1", "agent/task-1", base)
    (path / "change.txt").write_text("implementation\n")
    source.commit_all(path, "feat: implement locally")

    candidate = source.retain_candidate(path, "agent/task-1", expected_sha=None)
    assert source.retain_candidate(path, "agent/task-1", expected_sha=None) == candidate
    assert source.resolve_commit("agent/task-1") == candidate
    source.cleanup(path, success=True)

    assert not path.exists()
    assert source.resolve_commit("agent/task-1") == candidate
    assert source.resolve_commit("main") == base


def test_candidate_cas_rejects_unexpected_existing_branch(local_repo, tmp_path):
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    (path / "change.txt").write_text("implementation\n")
    source.commit_all(path, "feat: implement locally")
    git(local_repo, "branch", "agent/task-1", base)

    with pytest.raises(WorkspaceError, match="candidate branch changed"):
        source.retain_candidate(path, "agent/task-1", expected_sha=None)
    assert source.resolve_commit("agent/task-1") == base


@pytest.mark.parametrize("tamper", ["head", "ref", "managed", "metadata"])
def test_harness_cannot_change_controller_owned_state(local_repo, tmp_path, tamper):
    source = workshop(local_repo, tmp_path)
    path = source.provision("task-1", "agent/task-1", source.resolve_commit())
    before = source.capture_harness_state(path)
    if tamper == "head":
        git(path, "commit", "--allow-empty", "-qm", "unauthorized")
    elif tamper == "ref":
        git(path, "branch", "harness-created")
    elif tamper == "managed":
        target = path / ".machinist/runs/hidden.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}")
    else:
        git(path, "config", "core.fsmonitor", "evil-monitor")

    with pytest.raises(WorkspaceError):
        source.assert_harness_state(path, before)


def test_harness_edits_allowed_but_read_only_mutation_rejected(local_repo, tmp_path):
    source = workshop(local_repo, tmp_path)
    path = source.provision("task-1", "agent/task-1", source.resolve_commit())
    before = source.capture_harness_state(path)
    (path / "README.md").write_text("updated\n")

    source.assert_harness_state(path, before)
    with pytest.raises(WorkspaceError, match="read-only"):
        source.assert_harness_state(path, before, read_only=True)


def test_resume_authenticates_metadata_before_workshop_git(local_repo, tmp_path):
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    token = source.git_custody(path)
    (path / "README.md").write_text("retained operator change\n")
    resumed = workshop(local_repo, tmp_path)
    assert (
        resumed.resume(
            path, branch="agent/task-1", expected_sha=base, git_custody=token
        )
        == path
    )
    git(path, "config", "core.fsmonitor", "evil-monitor")
    calls = []
    resumed._workspace._runner = lambda *a, **k: calls.append((a, k))

    with pytest.raises(WorkspaceError, match="metadata changed"):
        resumed.resume(
            path, branch="agent/task-1", expected_sha=base, git_custody=token
        )
    assert calls == []


def candidate_fixture(local_repo, tmp_path):
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    (path / "README.md").write_text("implemented\n")
    source.commit_all(path, "feat: local candidate")
    candidate = source.retain_candidate(path, "agent/task-1", expected_sha=None)
    return source, base, candidate


def test_integrate_fast_forwards_exact_candidate_and_is_idempotent(
    local_repo, tmp_path
):
    source, base, candidate = candidate_fixture(local_repo, tmp_path)

    for _ in range(2):
        assert (
            source.integrate(
                "agent/task-1",
                base_branch="main",
                expected_base_sha=base,
                expected_candidate_sha=candidate,
            )
            == candidate
        )

    assert git(local_repo, "rev-parse", "HEAD") == candidate
    assert (local_repo / "README.md").read_text() == "implemented\n"
    assert git(local_repo, "status", "--porcelain") == ""


def test_integrate_rejects_dirty_checkout(local_repo, tmp_path):
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    (local_repo / "personal.txt").write_text("keep me\n")

    with pytest.raises(WorkspaceError, match="clean"):
        source.integrate(
            "agent/task-1",
            base_branch="main",
            expected_base_sha=base,
            expected_candidate_sha=candidate,
        )
    assert source.resolve_commit("main") == base
    assert (local_repo / "personal.txt").read_text() == "keep me\n"


def test_integrate_rejects_advanced_base(local_repo, tmp_path):
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    git(local_repo, "commit", "--allow-empty", "-qm", "parallel change")
    advanced = source.resolve_commit("main")

    with pytest.raises(WorkspaceError, match="base branch changed"):
        source.integrate(
            "agent/task-1",
            base_branch="main",
            expected_base_sha=base,
            expected_candidate_sha=candidate,
        )
    assert source.resolve_commit("main") == advanced


def test_integrate_reconciles_crash_after_ref_update(local_repo, tmp_path, monkeypatch):
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    checkout = source._checkout_candidate
    monkeypatch.setattr(source, "_checkout_candidate", lambda *args: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        source.integrate(
            "agent/task-1",
            base_branch="main",
            expected_base_sha=base,
            expected_candidate_sha=candidate,
        )
    assert source.resolve_commit("main") == candidate
    monkeypatch.setattr(source, "_checkout_candidate", checkout)

    source.integrate(
        "agent/task-1",
        base_branch="main",
        expected_base_sha=base,
        expected_candidate_sha=candidate,
    )
    assert git(local_repo, "status", "--porcelain") == ""
    assert (local_repo / "README.md").read_text() == "implemented\n"


def test_runtime_exclusion_is_local_idempotent_and_preserves_source(
    local_repo, tmp_path
):
    (local_repo / ".gitignore").write_text("*.cache\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "unconfigured runtime")
    source = workshop(local_repo, tmp_path)
    before_head = source.resolve_commit()

    source.ensure_runtime_ignored()
    source.ensure_runtime_ignored()
    runtime = local_repo / ".machinist/runs/local"
    runtime.mkdir(parents=True)
    (runtime / "config.yaml").write_text("mode: local\n")

    assert not source.has_changes(local_repo)
    assert source.resolve_commit() == before_head
    assert (local_repo / ".gitignore").read_text() == "*.cache\n"
    exclude = (local_repo / ".git/info/exclude").read_text()
    assert exclude.count("/.machinist/runs/") == 1


def test_runtime_exclusion_preflight_is_read_only_and_shared_with_setup(
    local_repo, tmp_path
):
    (local_repo / ".gitignore").write_text("*.cache\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "no runtime exclusion")
    source = workshop(local_repo, tmp_path)
    exclude = local_repo / ".git/info/exclude"
    before = exclude.read_bytes()

    assert source.runtime_exclusion_needs_update()
    assert exclude.read_bytes() == before
    assert not (local_repo / ".machinist").exists()
    source.ensure_runtime_ignored()
    assert not source.runtime_exclusion_needs_update()
    assert exclude.read_text().count("/.machinist/runs/") == 1


def test_read_at_commit_reads_exact_bounded_regular_file(local_repo, tmp_path):
    source = workshop(local_repo, tmp_path)
    sha = source.resolve_commit()
    (local_repo / "README.md").write_text("uncommitted content\n")

    assert source.read_at_commit(sha, "README.md", max_bytes=100) == "baseline\n"
    with pytest.raises(WorkspaceError, match="exceeds"):
        source.read_at_commit(sha, "README.md", max_bytes=3)
    with pytest.raises(WorkspaceError, match="relative"):
        source.read_at_commit(sha, "../README.md", max_bytes=100)


def test_publication_uses_exact_candidate_and_lease_without_touching_base(
    local_repo, tmp_path
):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(local_repo, "remote", "add", "origin", str(remote))
    source, base, candidate = candidate_fixture(local_repo, tmp_path)

    assert source.remote_sha("agent/task-1", origin_url=source.origin_url()) is None
    for _ in range(2):
        assert (
            source.push_candidate(
                "agent/task-1",
                expected_candidate_sha=candidate,
                expected_remote_sha=None,
                origin_url=str(remote),
            )
            == candidate
        )

    assert git(remote, "rev-parse", "agent/task-1") == candidate
    assert source.resolve_commit("main") == base


def test_publication_refuses_unexpected_remote_head(local_repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(local_repo, "remote", "add", "origin", str(remote))
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    git(local_repo, "push", "origin", f"{base}:refs/heads/agent/task-1")

    with pytest.raises(WorkspaceError, match="remote branch changed"):
        source.push_candidate(
            "agent/task-1",
            expected_candidate_sha=candidate,
            expected_remote_sha=None,
            origin_url=str(remote),
        )
    assert git(remote, "rev-parse", "agent/task-1") == base


@pytest.mark.parametrize(
    "url",
    [
        "https://token@example.com/owner/repo.git",
        "https://example.com/owner/repo?token=secret",
        "ext::sh -c evil",
        "http://example.com/owner/repo.git",
    ],
)
def test_publication_rejects_unsafe_or_credentialed_origins(local_repo, tmp_path, url):
    git(local_repo, "remote", "add", "origin", url)
    source = workshop(local_repo, tmp_path)

    with pytest.raises(WorkspaceError):
        source.origin_url()


def test_publication_rejects_split_push_destination(local_repo, tmp_path):
    git(local_repo, "remote", "add", "origin", "https://example.com/owner/repo.git")
    git(
        local_repo,
        "config",
        "remote.origin.pushurl",
        "https://elsewhere.test/other.git",
    )
    source = workshop(local_repo, tmp_path)

    with pytest.raises(WorkspaceError, match="pushurl"):
        source.origin_url()


def test_retention_cannot_target_another_branch_or_checked_out_task(
    local_repo, tmp_path
):
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    (path / "change.txt").write_text("implementation\n")
    source.commit_all(path, "feat: implementation")
    with pytest.raises(WorkspaceError, match="target branch"):
        source.retain_candidate(path, "main", expected_sha=base)
    git(local_repo, "checkout", "-qb", "agent/task-1")
    with pytest.raises(WorkspaceError, match="checked out"):
        source.retain_candidate(path, "agent/task-1", expected_sha=base)
    assert source.resolve_commit("main") == base
    assert source.resolve_commit("agent/task-1") == base


def test_missing_custody_and_wrong_controller_resume_fail_without_workshop_git(
    local_repo, tmp_path, monkeypatch
):
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    token = source.git_custody(path)
    token["controller_repository"] = str(tmp_path / "somewhere-else")
    calls = []
    monkeypatch.setattr(source._workspace, "_runner", lambda *a, **k: calls.append(a))
    with pytest.raises(WorkspaceError, match="another controller"):
        source.resume(path, branch="agent/task-1", expected_sha=base, git_custody=token)
    assert calls == []


def test_runtime_exclusion_refuses_symlink_target(local_repo, tmp_path):
    (local_repo / ".gitignore").write_text("*.cache\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "no runtime exclusion")
    target = tmp_path / "personal-exclude"
    target.write_text("keep me\n")
    exclude = local_repo / ".git/info/exclude"
    exclude.unlink()
    exclude.symlink_to(target)
    source = workshop(local_repo, tmp_path)
    with pytest.raises(WorkspaceError, match="safely exclude"):
        source.ensure_runtime_ignored()
    assert target.read_text() == "keep me\n"


def test_base_cas_rejects_change_between_preflight_and_integration(
    local_repo, tmp_path, monkeypatch
):
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    original = source._workspace._runner
    advanced = []

    def race(argv, **kwargs):
        if "update-ref" in argv and "refs/heads/main" in argv:
            git(local_repo, "commit", "--allow-empty", "-qm", "concurrent base")
            advanced.append(git(local_repo, "rev-parse", "HEAD"))
        return original(argv, **kwargs)

    monkeypatch.setattr(source._workspace, "_runner", race)
    with pytest.raises(WorkspaceError, match="update-ref"):
        source.integrate(
            "agent/task-1",
            base_branch="main",
            expected_base_sha=base,
            expected_candidate_sha=candidate,
        )
    assert source.resolve_commit("main") == advanced[0]
    assert (local_repo / "README.md").read_text() == "baseline\n"


def test_actual_push_lease_rejects_remote_race(local_repo, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(local_repo, "remote", "add", "origin", str(remote))
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    original = source._workspace._runner

    def race(argv, **kwargs):
        if "push" in argv:
            git(local_repo, "push", str(remote), f"{base}:refs/heads/agent/task-1")
        return original(argv, **kwargs)

    monkeypatch.setattr(source._workspace, "_runner", race)
    with pytest.raises(WorkspaceError, match="push"):
        source.push_candidate(
            "agent/task-1",
            expected_candidate_sha=candidate,
            expected_remote_sha=None,
            origin_url=str(remote),
        )
    assert git(remote, "rev-parse", "agent/task-1") == base


def test_runtime_exclusion_reports_overriding_repository_rules(local_repo, tmp_path):
    (local_repo / ".gitignore").write_text("!/.machinist/runs/\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "explicit unignore")
    source = workshop(local_repo, tmp_path)
    with pytest.raises(WorkspaceError, match="override"):
        source.ensure_runtime_ignored()


@pytest.mark.parametrize(
    "collision", ["same-file", "file-ancestor", "directory-ancestor"]
)
def test_integration_preserves_ignored_files_before_advancing_base(
    local_repo, tmp_path, collision
):
    (local_repo / ".gitignore").write_text("/.machinist/runs/\nprivate\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "ignore local user data")
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    candidate_file = path / (
        "private/tracked.txt" if collision == "file-ancestor" else "private"
    )
    candidate_file.parent.mkdir(parents=True, exist_ok=True)
    candidate_file.write_text("candidate configuration\n")
    git(path, "add", "-f", str(candidate_file))
    source.commit_all(path, "feat: candidate tracks formerly ignored path")
    candidate = source.retain_candidate(path, "agent/task-1", expected_sha=None)
    valuable = local_repo / (
        "private/valuable.txt" if collision == "directory-ancestor" else "private"
    )
    valuable.parent.mkdir(parents=True, exist_ok=True)
    valuable.write_text("valuable local configuration\n")

    with pytest.raises(WorkspaceError, match="untracked|ignored"):
        source.integrate(
            "agent/task-1",
            base_branch="main",
            expected_base_sha=base,
            expected_candidate_sha=candidate,
        )

    assert source.resolve_commit("main") == base
    assert valuable.read_text() == "valuable local configuration\n"


def test_integration_allows_unrelated_ignored_files(local_repo, tmp_path):
    (local_repo / ".gitignore").write_text("/.machinist/runs/\n.venv/\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "ignore local environment")
    source, base, candidate = candidate_fixture(local_repo, tmp_path)
    personal = local_repo / ".venv/valuable.txt"
    personal.parent.mkdir()
    personal.write_text("keep local environment\n")

    source.integrate(
        "agent/task-1",
        base_branch="main",
        expected_base_sha=base,
        expected_candidate_sha=candidate,
    )

    assert personal.read_text() == "keep local environment\n"


def test_gitlab_https_auth_is_explicit_host_scoped_and_ephemeral(
    local_repo, tmp_path, monkeypatch
):
    origin = "https://gitlab.example:8443/team/project.git"
    git(local_repo, "remote", "add", "origin", origin)
    monkeypatch.setenv("GITLAB_TOKEN", "ambient-other-host")
    monkeypatch.setenv("GITLAB_HOST", "other.example")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-cloud-secret")
    source = workshop(local_repo, tmp_path)
    config_before = (local_repo / ".git/config").read_text()
    calls = []
    requests = []
    original = source._workspace._runner

    def auth(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "glpat-local-test\n", "")

    def request(argv, **kwargs):
        if "ls-remote" in argv:
            requests.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(source._workspace, "_auth_runner", auth)
    monkeypatch.setattr(source._workspace, "_runner", request)
    source.resolve_commit()
    assert calls == []
    source.bind_publication_auth("gitlab", origin_url=origin)
    source.remote_sha("agent/task-1", origin_url=origin)

    assert calls[0][0] == [
        "glab",
        "config",
        "get",
        "token",
        "--host",
        "gitlab.example:8443",
    ]
    assert "GITLAB_TOKEN" not in calls[0][1]["env"]
    assert "AWS_SECRET_ACCESS_KEY" not in calls[0][1]["env"]
    environment = requests[0][1]["env"]
    assert "GITLAB_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert (
        environment["GIT_CONFIG_KEY_0"]
        == "http.https://gitlab.example:8443/team/project.git/.extraheader"
    )
    header = environment["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(header).decode() == "oauth2:glpat-local-test"
    assert "glpat-local-test" not in repr(requests[0][0])
    assert (local_repo / ".git/config").read_text() == config_before


@pytest.mark.parametrize(
    "payload",
    ["", "two\nsecrets", "x" * 16385],
    ids=["empty", "multiline", "oversized"],
)
def test_gitlab_auth_rejects_invalid_tokens_without_echo(
    local_repo, tmp_path, monkeypatch, payload
):
    origin = "https://gitlab.example/team/project.git"
    git(local_repo, "remote", "add", "origin", origin)
    source = workshop(local_repo, tmp_path)
    monkeypatch.setattr(
        source._workspace,
        "_auth_runner",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, payload, "hidden diagnostic"
        ),
    )
    source.bind_publication_auth("gitlab", origin_url=origin)
    with pytest.raises(WorkspaceError, match="GitLab authentication") as caught:
        source.remote_sha("agent/task-1", origin_url=origin)
    assert "hidden diagnostic" not in str(caught.value)
    assert "two" not in str(caught.value)


def test_unbound_https_origin_does_not_assume_gitlab_auth(
    local_repo, tmp_path, monkeypatch
):
    origin = "https://other.example/team/project.git"
    git(local_repo, "remote", "add", "origin", origin)
    source = workshop(local_repo, tmp_path)
    original = source._workspace._runner
    monkeypatch.setattr(
        source._workspace,
        "_auth_runner",
        lambda *a, **k: pytest.fail("unexpected authentication"),
    )

    def request(argv, **kwargs):
        if "ls-remote" in argv:
            assert "GIT_CONFIG_VALUE_0" not in kwargs["env"]
            return subprocess.CompletedProcess(argv, 0, "", "")
        return original(argv, **kwargs)

    monkeypatch.setattr(source._workspace, "_runner", request)
    source.remote_sha("agent/task-1", origin_url=origin)


def test_prepared_tree_identifies_exact_controller_commit_for_recovery(
    local_repo, tmp_path
):
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    (path / "README.md").write_text("verified implementation\n")
    (path / "added.txt").write_text("new file\n")

    tree = source.prepare_commit(path)
    assert source.head_sha(path) == base
    source.commit_all(path, "feat: exact verified implementation")
    identity = source.commit_identity(path)

    assert identity == {
        "sha": source.head_sha(path),
        "tree": tree,
        "parents": [base],
        "message": "feat: exact verified implementation",
    }
    assert not source.has_changes(path)


def test_recovery_preserves_ignored_data_created_after_ref_advancement(
    local_repo, tmp_path, monkeypatch
):
    (local_repo / ".gitignore").write_text("/.machinist/runs/\nprivate\n")
    git(local_repo, "add", ".gitignore")
    git(local_repo, "commit", "-qm", "ignore user data")
    source = workshop(local_repo, tmp_path)
    base = source.resolve_commit()
    path = source.provision("task-1", "agent/task-1", base)
    (path / "private").write_text("candidate\n")
    git(path, "add", "-f", "private")
    source.commit_all(path, "feat: private candidate path")
    candidate = source.retain_candidate(path, "agent/task-1", expected_sha=None)
    checkout = source._checkout_candidate

    def concurrent_local_data(*args):
        (local_repo / "private").write_text("valuable new local data\n")
        return checkout(*args)

    monkeypatch.setattr(source, "_checkout_candidate", concurrent_local_data)
    with pytest.raises(WorkspaceError, match="untracked or ignored"):
        source.integrate(
            "agent/task-1",
            base_branch="main",
            expected_base_sha=base,
            expected_candidate_sha=candidate,
        )
    assert source.resolve_commit("main") == candidate
    saved = tmp_path / "saved-local-data"
    (local_repo / "private").rename(saved)
    monkeypatch.setattr(source, "_checkout_candidate", checkout)

    source.integrate(
        "agent/task-1",
        base_branch="main",
        expected_base_sha=base,
        expected_candidate_sha=candidate,
    )

    assert saved.read_text() == "valuable new local data\n"
    assert (local_repo / "private").read_text() == "candidate\n"

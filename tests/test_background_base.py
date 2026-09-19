"""Remote-base admission against real local Git transports, without network."""

import subprocess
from pathlib import Path

import pytest

from machinist.config import WorkspaceConfig, WorkspaceStrategy
from machinist.forge import ForgeError
from machinist.local_workspace import LocalWorkspace
from machinist.workspace import WorkspaceError


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def remote_repository(tmp_path):
    author = tmp_path / "author"
    author.mkdir()
    git(author, "init", "-q", "-b", "main")
    git(author, "config", "user.name", "Background Test")
    git(author, "config", "user.email", "background@example.test")
    (author / "source.txt").write_text("original\n")
    git(author, "add", "source.txt")
    git(author, "commit", "-qm", "initial")
    original = git(author, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(author), str(remote))
    git(author, "remote", "add", "origin", str(remote))
    controller = tmp_path / "controller"
    git(tmp_path, "clone", "--no-hardlinks", str(remote), str(controller))
    source = LocalWorkspace(
        controller,
        WorkspaceConfig(root=tmp_path / "workshops", strategy=WorkspaceStrategy.CLONE),
    )
    return author, remote, controller, source, original


def advance_remote(author: Path, text: str = "remote advancement\n") -> str:
    (author / "source.txt").write_text(text)
    git(author, "add", "source.txt")
    git(author, "commit", "-qm", "advance remote")
    git(author, "push", "origin", "main")
    return git(author, "rev-parse", "HEAD")


def test_fetches_remote_ahead_without_advancing_any_controller_refs(remote_repository):
    author, _, controller, source, original = remote_repository
    remote_head = advance_remote(author)
    before_refs = git(controller, "show-ref")
    before_index = (controller / ".git" / "index").read_bytes()
    fetch_head = controller / ".git" / "FETCH_HEAD"
    fetch_head.write_text("previous operator fetch\n")

    admitted = source.fetch_background_base("main")

    assert admitted == remote_head != original
    assert source.resolve_commit(admitted) == remote_head
    assert git(controller, "show", f"{admitted}:source.txt") == "remote advancement"
    assert git(controller, "show-ref") == before_refs
    assert git(controller, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(controller, "rev-parse", "HEAD") == original
    assert (controller / "source.txt").read_text() == "original\n"
    assert (controller / ".git" / "index").read_bytes() == before_index
    assert fetch_head.read_text() == "previous operator fetch\n"
    assert git(controller, "status", "--porcelain") == ""


def test_fetched_base_can_provision_exact_clone_without_checkout_update(
    remote_repository,
):
    author, _, controller, source, original = remote_repository
    remote_head = advance_remote(author)
    admitted = source.fetch_background_base("main")

    workshop = source.provision("T1", "agent/task-1", admitted)

    assert source.head_sha(workshop) == remote_head
    assert (workshop / "source.txt").read_text() == "remote advancement\n"
    assert (workshop / ".git").is_dir()
    assert git(workshop, "remote") == ""
    assert git(controller, "rev-parse", "HEAD") == original


def test_fetch_does_not_import_remote_tags_or_create_fetch_head(remote_repository):
    author, _, controller, source, _ = remote_repository
    remote_head = advance_remote(author)
    git(author, "tag", "remote-release", remote_head)
    git(author, "push", "origin", "refs/tags/remote-release")
    fetch_head = controller / ".git" / "FETCH_HEAD"
    assert not fetch_head.exists()

    assert source.fetch_background_base("main") == remote_head

    assert git(controller, "tag", "--list") == ""
    assert not fetch_head.exists()


@pytest.mark.parametrize("stage", ["before_fetch", "after_fetch"])
def test_remote_branch_race_fails_without_moving_controller(
    remote_repository, monkeypatch, stage
):
    author, _, controller, source, original = remote_repository
    advance_remote(author)
    before_refs = git(controller, "show-ref")
    network_git = source._network_git

    def racing_network_git(*args):
        if args[0] == "fetch" and stage == "before_fetch":
            advance_remote(author, "racing before fetch\n")
        result = network_git(*args)
        if args[0] == "fetch" and stage == "after_fetch":
            advance_remote(author, "racing after fetch\n")
        return result

    monkeypatch.setattr(source, "_network_git", racing_network_git)
    with pytest.raises(WorkspaceError, match="base changed during fetch"):
        source.fetch_background_base("main")

    assert git(controller, "rev-parse", "HEAD") == original
    assert git(controller, "show-ref") == before_refs
    assert (controller / "source.txt").read_text() == "original\n"


@pytest.mark.parametrize(
    "branch",
    ["", "--upload-pack=evil", "main:other", "main\nnext", "../main", "main~1"],
)
def test_invalid_base_ref_rejected_before_any_remote_operation(
    remote_repository, monkeypatch, branch
):
    _, _, _, source, _ = remote_repository
    calls = []
    monkeypatch.setattr(source, "_network_git", lambda *args: calls.append(args))
    with pytest.raises(ForgeError, match="invalid change branch or base"):
        source.fetch_background_base(branch)
    assert calls == []


def test_missing_base_never_fetches_or_creates_local_ref(
    remote_repository, monkeypatch
):
    _, _, controller, source, _ = remote_repository
    network_git = source._network_git
    calls = []

    def observed_network_git(*args):
        calls.append(args)
        return network_git(*args)

    monkeypatch.setattr(source, "_network_git", observed_network_git)
    before_refs = git(controller, "show-ref")
    with pytest.raises(WorkspaceError, match="base branch is missing"):
        source.fetch_background_base("missing-branch")
    assert [args[0] for args in calls] == ["ls-remote"]
    assert git(controller, "show-ref") == before_refs


def test_fetch_failure_is_not_hidden_by_preexisting_local_object(
    remote_repository, monkeypatch
):
    _, _, controller, source, original = remote_repository
    network_git = source._network_git

    def failed_fetch(*args):
        if args[0] == "fetch":
            raise WorkspaceError("transport failed")
        return network_git(*args)

    monkeypatch.setattr(source, "_network_git", failed_fetch)
    with pytest.raises(WorkspaceError, match="transport failed"):
        source.fetch_background_base("main")
    assert git(controller, "rev-parse", "HEAD") == original


def test_changed_controller_transport_blocks_fetch_before_remote_access(
    remote_repository, monkeypatch
):
    _, _, controller, source, _ = remote_repository
    git(
        controller, "remote", "set-url", "origin", "https://example.test/other/repo.git"
    )
    calls = []
    monkeypatch.setattr(source, "_network_git", lambda *args: calls.append(args))

    with pytest.raises(WorkspaceError, match="metadata changed"):
        source.fetch_background_base("main")
    assert calls == []

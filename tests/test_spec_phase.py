"""Tests for Phase 1 orchestration: issue → spec → branch → draft PR."""

import pytest

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue, PullRequest
from machinist.phases.spec import (
    SpecPhaseError,
    preview_spec_phase,
    render_spec_prompt,
    run_spec_phase,
)

ISSUE = Issue(
    number=42,
    title="Add dark mode",
    body="Users want dark mode. Cost: $0. Use {theme} tokens.",
    url="https://github.com/vscarpenter/demo/issues/42",
    labels=["agent-task"],
)


class FakeGitHub:
    def __init__(self, issue=ISSUE, existing_pr=None):
        self.calls = []
        self.issue = issue
        self.existing_pr = existing_pr

    def get_issue(self, number):
        self.calls.append(("get_issue", number))
        return self.issue

    def default_branch(self):
        self.calls.append(("default_branch",))
        return "main"

    def ensure_label(self, name, *, color, description):
        self.calls.append(("ensure_label", name))

    def create_draft_pr(self, *, branch, base, title, body):
        self.calls.append(("create_draft_pr", branch, base, title, body))
        return DraftPR(number=57, url="https://github.com/vscarpenter/demo/pull/57")

    def pr_for_branch(self, branch):
        self.calls.append(("pr_for_branch", branch))
        return self.existing_pr

    def reopen_pr(self, number):
        self.calls.append(("reopen_pr", number))

    def update_pr(self, number, *, title=None, body=None):
        self.calls.append(("update_pr", number, title, body))

    def remove_pr_label(self, number, label):
        self.calls.append(("remove_pr_label", number, label))


class FakeHarness:
    name = "fake"

    def __init__(self, spec_text="## Spec\n\nDo the thing.\n", error=None):
        self.spec_text = spec_text
        self.error = error
        self.prompts = []

    def generate_spec(self, prompt, cwd):
        self.prompts.append((prompt, cwd))
        if self.error:
            raise self.error
        return self.spec_text


class FakeWorkspace:
    def __init__(self, tmp_path):
        self.path = tmp_path / "ws"
        self.calls = []
        self.dirty = False

    def provision(self, task, branch, base_ref):
        self.calls.append(("provision", task, branch, base_ref))
        self.path.mkdir()
        return self.path

    def commit_all(self, path, message):
        self.calls.append(("commit_all", message))

    def push(self, path, branch, expected_sha=None):
        call = (
            ("push", branch) if expected_sha is None else ("push", branch, expected_sha)
        )
        self.calls.append(call)

    def has_changes(self, path):
        return self.dirty

    def head_sha(self, path):
        return "b" * 40

    def cleanup(self, path, *, success):
        self.calls.append(("cleanup", success))


def test_render_spec_prompt_includes_issue_and_survives_special_chars():
    prompt = render_spec_prompt(ISSUE)
    assert "#42" in prompt
    assert "Add dark mode" in prompt
    assert "Cost: $0. Use {theme} tokens." in prompt


def test_render_spec_prompt_handles_empty_body():
    issue = Issue(number=7, title="t", body="", url="u")
    assert "no description" in render_spec_prompt(issue).lower()


def test_render_spec_prompt_delimits_repository_instructions():
    prompt = render_spec_prompt(ISSUE, "Prefer the public API.")

    assert "BEGIN REPOSITORY INSTRUCTIONS" in prompt
    assert "Prefer the public API." in prompt


def test_happy_path_creates_spec_branch_and_draft_pr(tmp_path):
    github, harness = FakeGitHub(), FakeHarness()
    workspace = FakeWorkspace(tmp_path)
    config = MachinistConfig()

    pr = run_spec_phase(42, config, github=github, harness=harness, workspace=workspace)

    assert pr.number == 57
    assert ("provision", "issue-42", "agent/issue-42", "origin/main") in workspace.calls

    spec_file = workspace.path / ".machinist" / "specs" / "issue-42-spec.md"
    assert spec_file.read_text() == harness.spec_text

    commit = next(c for c in workspace.calls if c[0] == "commit_all")
    assert "#42" in commit[1]
    assert ("push", "agent/issue-42") in workspace.calls
    assert ("ensure_label", "machinist:approved") in github.calls
    assert ("cleanup", True) in workspace.calls

    _, branch, base, title, body = next(
        c for c in github.calls if c[0] == "create_draft_pr"
    )
    assert branch == "agent/issue-42"
    assert base == "main"
    assert "Add dark mode" in title
    assert "Closes #42" in body
    assert "machinist:approved" in body
    assert "/machinist-execute" in body
    # Dogfood UX finding: solo devs reach for GitHub's review Approve button,
    # which GitHub blocks on their own PRs. The body must head that off.
    assert "Approve button" in body
    # Dogfood UX finding 2 (both live lifecycles): users instinctively click
    # "Ready for review", which blocks the daemon (it implements drafts only).
    assert "leave this PR as a draft" in body


def test_branch_prefix_comes_from_config(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    config = MachinistConfig.model_validate({"workspace": {"branch_prefix": "bot/"}})

    run_spec_phase(
        42, config, github=FakeGitHub(), harness=FakeHarness(), workspace=workspace
    )

    assert ("provision", "issue-42", "bot/issue-42", "origin/main") in workspace.calls


def test_preview_generates_read_only_spec_without_git_or_github_delivery(tmp_path):
    github, harness = FakeGitHub(), FakeHarness()
    workspace = FakeWorkspace(tmp_path)

    preview = preview_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=harness,
        workspace=workspace,
    )

    assert preview == harness.spec_text
    provision = next(call for call in workspace.calls if call[0] == "provision")
    assert provision[1].startswith("preview-issue-42-")
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert not any(
        call[0] in {"ensure_label", "create_draft_pr", "update_pr"}
        for call in github.calls
    )
    assert ("cleanup", True) in workspace.calls


def test_revise_updates_existing_draft_pr_and_invalidates_approval(tmp_path):
    existing = PullRequest(
        number=57,
        title="Spec: old",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        is_draft=True,
        head_sha="a" * 40,
        labels=["machinist:approved"],
    )
    github = FakeGitHub(existing_pr=existing)
    workspace = FakeWorkspace(tmp_path)

    result = run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(spec_text="## Revised\nDo the safer thing.\n"),
        workspace=workspace,
        revise=True,
    )

    assert result == DraftPR(number=57, url=existing.url)
    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls
    assert ("remove_pr_label", 57, "machinist:approved") in github.calls
    assert any(call[0] == "update_pr" for call in github.calls)
    assert not any(call[0] == "create_draft_pr" for call in github.calls)


def test_revise_reopens_a_closed_spec_pr_after_successful_push(tmp_path):
    existing = PullRequest(
        number=57,
        title="Spec: old",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        is_draft=True,
        head_sha="a" * 40,
        state="CLOSED",
    )
    github = FakeGitHub(existing_pr=existing)

    run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(),
        workspace=FakeWorkspace(tmp_path),
        revise=True,
    )

    assert ("reopen_pr", 57) in github.calls


def test_revise_refuses_a_merged_spec_pr(tmp_path):
    existing = PullRequest(
        number=57,
        title="Spec: old",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        is_draft=False,
        head_sha="a" * 40,
        state="MERGED",
    )

    with pytest.raises(SpecPhaseError, match="merged"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(existing_pr=existing),
            harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path),
            revise=True,
        )


def test_empty_spec_output_fails_and_keeps_nothing(tmp_path):
    github = FakeGitHub()
    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(SpecPhaseError, match="empty spec"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(spec_text="  \n"),
            workspace=workspace,
        )

    assert ("cleanup", False) in workspace.calls
    assert not any(c[0] == "create_draft_pr" for c in github.calls)


def test_oversized_issue_input_is_rejected_before_harness_runs(tmp_path):
    github = FakeGitHub()
    github.issue = Issue(
        number=42,
        title="Add dark mode",
        body="x" * 50_001,
        url="https://github.com/x/y/issues/42",
        labels=["agent-task"],
    )
    harness = FakeHarness()

    with pytest.raises(SpecPhaseError, match="body is too large"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=harness,
            workspace=FakeWorkspace(tmp_path),
        )

    assert harness.prompts == []


def test_oversized_spec_output_is_rejected_before_write_or_push(tmp_path):
    github = FakeGitHub()
    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(SpecPhaseError, match="spec is too large"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(spec_text="x" * 100_001),
            workspace=workspace,
        )

    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)


def test_harness_failure_cleans_up_and_propagates(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    boom = RuntimeError("harness exploded")

    with pytest.raises(RuntimeError, match="harness exploded"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=FakeHarness(error=boom),
            workspace=workspace,
        )

    assert ("cleanup", False) in workspace.calls


def test_spec_harness_repository_mutation_is_rejected(tmp_path):
    workspace = FakeWorkspace(tmp_path)

    class MutatingHarness(FakeHarness):
        def generate_spec(self, prompt, cwd):
            (cwd / "oops.py").write_text("changed\n")
            workspace.dirty = True
            return super().generate_spec(prompt, cwd)

    with pytest.raises(SpecPhaseError, match="read-only"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=MutatingHarness(),
            workspace=workspace,
        )

    assert not any(call[0] == "commit_all" for call in workspace.calls)
    assert ("cleanup", False) in workspace.calls

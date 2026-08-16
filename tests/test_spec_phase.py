"""Tests for Phase 1 orchestration: issue → spec → branch → draft PR."""

import pytest

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue
from machinist.phases.spec import SpecPhaseError, render_spec_prompt, run_spec_phase


ISSUE = Issue(
    number=42,
    title="Add dark mode",
    body="Users want dark mode. Cost: $0. Use {theme} tokens.",
    url="https://github.com/vscarpenter/demo/issues/42",
    labels=["agent-task"],
)


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def get_issue(self, number):
        self.calls.append(("get_issue", number))
        return ISSUE

    def default_branch(self):
        self.calls.append(("default_branch",))
        return "main"

    def ensure_label(self, name, *, color, description):
        self.calls.append(("ensure_label", name))

    def create_draft_pr(self, *, branch, base, title, body):
        self.calls.append(("create_draft_pr", branch, base, title, body))
        return DraftPR(number=57, url="https://github.com/vscarpenter/demo/pull/57")


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

    def provision(self, task, branch, base_ref):
        self.calls.append(("provision", task, branch, base_ref))
        self.path.mkdir()
        return self.path

    def commit_all(self, path, message):
        self.calls.append(("commit_all", message))

    def push(self, path, branch):
        self.calls.append(("push", branch))

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

    _, branch, base, title, body = next(c for c in github.calls if c[0] == "create_draft_pr")
    assert branch == "agent/issue-42"
    assert base == "main"
    assert "Add dark mode" in title
    assert "Closes #42" in body
    assert "machinist:approved" in body
    assert "/machinist-execute" in body


def test_branch_prefix_comes_from_config(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    config = MachinistConfig.model_validate({"workspace": {"branch_prefix": "bot/"}})

    run_spec_phase(42, config, github=FakeGitHub(), harness=FakeHarness(), workspace=workspace)

    assert ("provision", "issue-42", "bot/issue-42", "origin/main") in workspace.calls


def test_empty_spec_output_fails_and_keeps_nothing(tmp_path):
    github = FakeGitHub()
    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(SpecPhaseError, match="empty spec"):
        run_spec_phase(
            42, MachinistConfig(),
            github=github, harness=FakeHarness(spec_text="  \n"), workspace=workspace,
        )

    assert ("cleanup", False) in workspace.calls
    assert not any(c[0] == "create_draft_pr" for c in github.calls)


def test_harness_failure_cleans_up_and_propagates(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    boom = RuntimeError("harness exploded")

    with pytest.raises(RuntimeError, match="harness exploded"):
        run_spec_phase(
            42, MachinistConfig(),
            github=FakeGitHub(), harness=FakeHarness(error=boom), workspace=workspace,
        )

    assert ("cleanup", False) in workspace.calls

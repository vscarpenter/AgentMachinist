"""Tests for Phase 3 orchestration: approved spec → implementation → ready PR."""

import subprocess

import pytest

from machinist.config import MachinistConfig
from machinist.github import PullRequest
from machinist.phases.execute import (
    ExecutePhaseError,
    render_implement_prompt,
    run_execute_phase,
)


def make_pr(*, draft=True, labels=("machinist:approved",)):
    return PullRequest(
        number=57,
        title="Spec: Add dark mode (#42)",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        head_sha="a" * 40,
        is_draft=draft,
        labels=list(labels),
    )


class FakeGitHub:
    def __init__(self, prs=()):
        self._prs = list(prs)
        self.calls = []

    def open_machinist_prs(self, prefix):
        self.calls.append(("open_machinist_prs", prefix))
        return self._prs

    def default_branch(self):
        return "main"

    def approval_sha(self, number):
        return "a" * 40

    def mark_ready(self, number):
        self.calls.append(("mark_ready", number))


class FakeHarness:
    name = "fake"

    def __init__(self, on_implement=None, error=None):
        self.prompts = []
        self.on_implement = on_implement
        self.error = error

    def implement(self, prompt, cwd):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        if self.on_implement:
            self.on_implement(cwd)
        return "done"


class FakeWorkspace:
    def __init__(self, tmp_path, spec_text="## Spec\nDo the thing.\n"):
        self.path = tmp_path / "ws"
        self.calls = []
        self._dirty = False
        self._spec_text = spec_text

    def provision(self, task, branch, base_ref):
        self.calls.append(("provision", task, branch, base_ref))
        spec_dir = self.path / ".machinist" / "specs"
        spec_dir.mkdir(parents=True)
        if self._spec_text is not None:
            (spec_dir / "issue-42-spec.md").write_text(self._spec_text)
        return self.path

    def has_changes(self, path):
        return self._dirty

    def commit_all(self, path, message):
        self.calls.append(("commit_all", message))

    def push(self, path, branch):
        self.calls.append(("push", branch))

    def cleanup(self, path, *, success):
        self.calls.append(("cleanup", success))


def touch_file(workspace):
    def _apply(cwd):
        (cwd / "impl.py").write_text("code\n")
        workspace._dirty = True
    return _apply


def passing_tests(args, **kwargs):
    return subprocess.CompletedProcess(args, 0, "5 passed", "")


def failing_tests(args, **kwargs):
    return subprocess.CompletedProcess(args, 1, "", "2 failed: test_x, test_y")


def config_with_tests(command="pytest -q"):
    return MachinistConfig.model_validate({"tests": {"command": command}})


def test_render_implement_prompt_embeds_spec():
    prompt = render_implement_prompt(42, "## Spec\nBuild $it with {braces}\n")
    assert "#42" in prompt
    assert "Build $it with {braces}" in prompt
    assert ".machinist" in prompt  # forbids touching pipeline files


def test_happy_path_implements_tests_pushes_and_marks_ready(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))
    ran = {}

    def test_runner(command, **kwargs):
        ran["command"] = command
        ran["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(command, 0, "ok", "")

    pr = run_execute_phase(
        42, config_with_tests(),
        github=github, harness=harness, workspace=workspace, test_runner=test_runner,
    )

    assert pr.number == 57
    assert ("provision", "issue-42", "agent/issue-42", "origin/main") in workspace.calls
    assert "Do the thing." in harness.prompts[0]
    assert ran == {"command": "pytest -q", "cwd": workspace.path}
    commit = next(c for c in workspace.calls if c[0] == "commit_all")
    assert "#42" in commit[1]
    assert ("push", "agent/issue-42") in workspace.calls
    assert ("mark_ready", 57) in github.calls
    assert ("cleanup", True) in workspace.calls


def test_no_open_pr_for_branch_refuses(tmp_path):
    with pytest.raises(ExecutePhaseError, match="machinist spec 42"):
        run_execute_phase(
            42, MachinistConfig(),
            github=FakeGitHub(prs=[]), harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path), test_runner=passing_tests,
        )


def test_unapproved_pr_refuses_and_names_the_label(tmp_path):
    github = FakeGitHub(prs=[make_pr(labels=())])

    with pytest.raises(ExecutePhaseError, match="machinist:approved"):
        run_execute_phase(
            42, MachinistConfig(),
            github=github, harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path), test_runner=passing_tests,
        )

    assert not any(c[0] == "mark_ready" for c in github.calls)


def test_missing_approval_sha_refuses(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    github.approval_sha = lambda number: None

    with pytest.raises(ExecutePhaseError, match="approval evidence"):
        run_execute_phase(
            42, MachinistConfig(), github=github, harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path), test_runner=passing_tests,
        )


def test_stale_approval_sha_refuses(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    github.approval_sha = lambda number: "b" * 40

    with pytest.raises(ExecutePhaseError, match="changed after approval"):
        run_execute_phase(
            42, MachinistConfig(), github=github, harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path), test_runner=passing_tests,
        )


def test_already_implemented_pr_refuses_without_force(tmp_path):
    github = FakeGitHub(prs=[make_pr(draft=False)])
    harness = FakeHarness()

    with pytest.raises(ExecutePhaseError, match="--force"):
        run_execute_phase(
            42, MachinistConfig(),
            github=github, harness=harness,
            workspace=FakeWorkspace(tmp_path), test_runner=passing_tests,
        )

    assert harness.prompts == []


def test_force_reimplements_a_ready_pr(tmp_path):
    github = FakeGitHub(prs=[make_pr(draft=False)])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    pr = run_execute_phase(
        42, MachinistConfig(),
        github=github, harness=harness,
        workspace=workspace, test_runner=passing_tests, force=True,
    )

    assert pr.number == 57
    assert ("push", "agent/issue-42") in workspace.calls


def test_missing_spec_file_fails_before_harness_runs(tmp_path):
    harness = FakeHarness()
    workspace = FakeWorkspace(tmp_path, spec_text=None)

    with pytest.raises(ExecutePhaseError, match="spec"):
        run_execute_phase(
            42, MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]), harness=harness,
            workspace=workspace, test_runner=passing_tests,
        )

    assert harness.prompts == []
    assert ("cleanup", False) in workspace.calls


def test_no_changes_from_harness_fails(tmp_path):
    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(ExecutePhaseError, match="no changes"):
        run_execute_phase(
            42, MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]), harness=FakeHarness(),
            workspace=workspace, test_runner=passing_tests,
        )

    assert not any(c[0] == "push" for c in workspace.calls)


def test_failing_test_gate_keeps_workspace_and_never_pushes(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    with pytest.raises(ExecutePhaseError, match="test_x"):
        run_execute_phase(
            42, config_with_tests(),
            github=github, harness=harness,
            workspace=workspace, test_runner=failing_tests,
        )

    assert not any(c[0] in ("commit_all", "push") for c in workspace.calls)
    assert not any(c[0] == "mark_ready" for c in github.calls)
    assert ("cleanup", False) in workspace.calls


def test_null_test_command_skips_the_gate(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    def exploding_runner(*args, **kwargs):
        raise AssertionError("test gate should not run")

    pr = run_execute_phase(
        42, MachinistConfig(),  # tests.command is null by default
        github=FakeGitHub(prs=[make_pr()]), harness=harness,
        workspace=workspace, test_runner=exploding_runner,
    )

    assert pr.number == 57
    assert ("push", "agent/issue-42") in workspace.calls

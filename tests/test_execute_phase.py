"""Tests for Phase 3 orchestration: approved spec → implementation → ready PR."""

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from machinist.config import MachinistConfig
from machinist.github import PullRequest
from machinist.lifecycle import Phase, RunStatus, TaskLifecycle
from machinist.phases.execute import (
    ExecutePhaseCancelled,
    ExecutePhaseError,
    render_implement_prompt,
    run_execute_phase,
)
from machinist.process import ProcessCancelledError, ProcessStragglerError
from machinist.workspace import Workspace


def make_pr(*, draft=True, labels=("machinist:approved",), base="main"):
    return PullRequest(
        number=57,
        title="Spec: Add dark mode (#42)",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        head_sha="a" * 40,
        is_draft=draft,
        labels=list(labels),
        base=base,
    )


class FakeGitHub:
    def __init__(self, prs=()):
        self._prs = list(prs)
        self.calls = []
        self._next_comment_id = 123
        self.delivery_head_sha = "c" * 40
        self._ready_numbers = set()

    def open_machinist_prs(self, prefix):
        self.calls.append(("open_machinist_prs", prefix))
        return self._prs

    def default_branch(self):
        return "main"

    def pr_for_branch(self, branch):
        self.calls.append(("pr_for_branch", branch))
        candidate = next((pr for pr in self._prs if pr.branch == branch), None)
        return (
            None
            if candidate is None
            else replace(
                candidate,
                head_sha=self.delivery_head_sha,
                is_draft=(
                    False
                    if candidate.number in self._ready_numbers
                    else candidate.is_draft
                ),
            )
        )

    def approval_sha(self, number):
        return "a" * 40

    def mark_ready(self, number):
        self.calls.append(("mark_ready", number))
        self._ready_numbers.add(number)

    def upsert_pr_comment(self, number, body, *, comment_id=None):
        self.calls.append(("upsert_pr_comment", number, body, comment_id))
        return comment_id or self._next_comment_id


class FakeHarness:
    name = "fake"

    def __init__(self, on_implement=None, error=None, report="done"):
        self.prompts = []
        self.on_implement = on_implement
        self.error = error
        self.report = report

    def implement(self, prompt, cwd):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        if self.on_implement:
            self.on_implement(cwd)
        return self.report


class FakeWorkspace:
    def __init__(self, tmp_path, spec_text="## Spec\nDo the thing.\n"):
        self.path = tmp_path / "ws"
        self._tmp_path = tmp_path
        self.calls = []
        self._dirty = False
        self._spec_text = spec_text
        self._committed = False
        self._head_override = None
        self._remote_sha = "a" * 40
        self._machinist_changed = False
        self._changed_files = None
        self._snapshot = "clean"
        self.on_push = None
        self.push_error = None

    def repository_identity(self):
        return "x/y"

    def repository_target(self):
        return "github.com", "x/y"

    def provision(self, task, branch, base_ref, *, attempt=None):
        call = ("provision", task, branch, base_ref)
        if attempt is not None:
            call += (attempt,)
            self.path = self._tmp_path / f"ws-attempt-{attempt}"
        self.calls.append(call)
        self._prepare_path(self.path)
        return self.path

    def resume(self, path, *, branch, expected_sha):
        self.calls.append(("resume", Path(path), branch, expected_sha))
        self.path = Path(path)
        self._prepare_path(self.path)
        actual = self.head_sha(self.path)
        if actual != expected_sha:
            raise RuntimeError(f"expected {expected_sha}, found {actual}")
        return self.path

    def _prepare_path(self, path):
        spec_dir = path / ".machinist" / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        if self._spec_text is not None:
            (spec_dir / "issue-42-spec.md").write_text(self._spec_text)

    def has_changes(self, path):
        return self._dirty

    def changed_files(self, path):
        if self._changed_files is not None:
            return list(self._changed_files)
        if self._machinist_changed:
            return [".machinist/controller-owned.txt"]
        return ["impl.py"] if self._dirty else []

    def change_snapshot(self, path):
        return self._snapshot

    def commit_all(self, path, message):
        self.calls.append(("commit_all", message))
        self._committed = True
        self._dirty = False
        self._changed_files = []

    def push(self, path, branch, expected_sha=None):
        self.calls.append(("push", branch, expected_sha))
        if self.on_push:
            self.on_push()
        if self.push_error:
            raise self.push_error
        self._remote_sha = self.head_sha(path)

    def head_sha(self, path):
        return self._head_override or ("c" * 40 if self._committed else "a" * 40)

    def remote_sha(self, path, branch):
        return self._remote_sha

    def path_changed(self, path, relative):
        return self._machinist_changed

    def cleanup(self, path, *, success):
        self.calls.append(("cleanup", success))


class FakeClaim:
    def __init__(self, tmp_path, *, attempt=1, previous_evidence=None):
        self.attempt = attempt
        self.previous_evidence = dict(previous_evidence or {})
        self.evidence = dict(self.previous_evidence)
        self.checkpoints = []
        self._log_root = tmp_path / "run-logs" / f"attempt-{attempt}"

    def log_path(self, name):
        self._log_root.mkdir(parents=True, exist_ok=True)
        return self._log_root / name

    def checkpoint(self, **evidence):
        self.evidence.update(evidence)
        self.checkpoints.append(dict(evidence))


def touch_file(workspace):
    def _apply(cwd):
        (cwd / "impl.py").write_text("code\n")
        workspace._dirty = True

    return _apply


def passing_tests(args, **kwargs):
    return subprocess.CompletedProcess(args, 0, "5 passed", "")


def failing_tests(args, **kwargs):
    return subprocess.CompletedProcess(args, 1, "", "2 failed: test_x, test_y")


def real_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def real_approved_execute_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        capture_output=True,
        check=True,
    )
    controller = tmp_path / "controller"
    subprocess.run(
        ["git", "clone", str(origin), str(controller)],
        capture_output=True,
        check=True,
    )
    real_git(controller, "config", "user.email", "test@example.com")
    real_git(controller, "config", "user.name", "Test User")
    (controller / "README.md").write_text("hello\n")
    real_git(controller, "add", "README.md")
    real_git(controller, "commit", "-m", "initial")
    real_git(controller, "push", "origin", "main")
    real_git(controller, "checkout", "-b", "agent/issue-42")
    spec = controller / ".machinist" / "specs" / "issue-42-spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("## Approved spec\nImplement safely.\n")
    real_git(controller, "add", str(spec.relative_to(controller)))
    real_git(controller, "commit", "-m", "approved spec")
    real_git(controller, "push", "origin", "agent/issue-42")
    approved_sha = real_git(controller, "rev-parse", "HEAD")
    real_git(controller, "checkout", "main")
    return controller, origin, approved_sha


def config_with_tests(command="pytest -q"):
    return MachinistConfig.model_validate({"tests": {"command": command}})


def config_with_named_gate(command="lint"):
    return MachinistConfig.model_validate(
        {
            "verification": {
                "gates": [
                    {
                        "name": "lint",
                        "command": command,
                        "timeout_minutes": 2,
                        "mutation_policy": "forbid",
                    }
                ]
            }
        }
    )


def test_render_implement_prompt_embeds_spec():
    prompt = render_implement_prompt(42, "## Spec\nBuild $it with {braces}\n")
    assert "#42" in prompt
    assert "Build $it with {braces}" in prompt
    assert ".machinist" in prompt  # forbids touching pipeline files


def test_render_implement_prompt_delimits_repository_instructions():
    prompt = render_implement_prompt(
        42,
        "## Spec\nDo it.\n",
        instructions="Preserve public APIs.",
    )

    assert "BEGIN REPOSITORY INSTRUCTIONS" in prompt
    assert "Preserve public APIs." in prompt


def test_render_implement_prompt_appends_bounded_operator_feedback():
    prompt = render_implement_prompt(
        42,
        "## Spec\nBuild it\n",
        "Keep the public API and add a migration note.",
    )

    assert "--- BEGIN OPERATOR FEEDBACK ---" in prompt
    assert "Keep the public API" in prompt
    assert "--- END OPERATOR FEEDBACK ---" in prompt

    with pytest.raises(ExecutePhaseError, match="feedback is too large"):
        render_implement_prompt(42, "spec", "x" * 50_001)

    with pytest.raises(ExecutePhaseError, match="non-whitespace"):
        render_implement_prompt(42, "spec", "  \n\t  ")


def test_render_implement_prompt_lists_gates_with_feedback_loop_rules():
    gates = config_with_tests("uv run pytest").resolved_verification_gates()

    prompt = render_implement_prompt(42, "## Spec\nDo it.\n", gates=gates)

    assert "## Verifying your work" in prompt
    assert "`uv run pytest`" in prompt
    assert "(required)" in prompt
    assert "never delete, skip, or weaken a test" in prompt


def test_render_implement_prompt_marks_advisory_gates():
    config = MachinistConfig.model_validate(
        {
            "verification": {
                "gates": [{"name": "lint", "command": "ruff check .", "required": False}]
            }
        }
    )

    prompt = render_implement_prompt(
        42, "spec", gates=config.resolved_verification_gates()
    )

    assert "lint (advisory): `ruff check .`" in prompt


def test_render_implement_prompt_omits_verification_section_without_gates():
    assert "Verifying your work" not in render_implement_prompt(42, "spec")


def test_execute_grants_gate_commands_and_verification_prompt(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    run_execute_phase(
        42,
        config_with_tests("uv run pytest"),
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
    )

    assert harness.allowed_commands == ("uv run pytest",)
    assert "## Verifying your work" in harness.prompts[0]


def test_execute_withholds_gate_commands_when_disabled(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))
    config = MachinistConfig.model_validate(
        {
            "tests": {"command": "uv run pytest"},
            "verification": {"harness_may_run_gates": False},
        }
    )

    run_execute_phase(
        42,
        config,
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
    )

    assert harness.allowed_commands == ()
    assert "Verifying your work" not in harness.prompts[0]


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
        42,
        config_with_tests(),
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=test_runner,
    )

    assert pr.number == 57
    assert ("provision", "issue-42", "agent/issue-42", "origin/main") in workspace.calls
    assert "Do the thing." in harness.prompts[0]
    assert ran == {"command": "pytest -q", "cwd": workspace.path}
    commit = next(c for c in workspace.calls if c[0] == "commit_all")
    assert "#42" in commit[1]
    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls
    assert ("mark_ready", 57) in github.calls
    assert github.repo == "x/y"
    assert github.repo_host == "github.com"
    comment = next(call for call in github.calls if call[0] == "upsert_pr_comment")
    assert "Implementation complete" in comment[2]
    assert "c" * 12 in comment[2]
    assert "Effective harness: `fake`" in comment[2]
    assert "done" in comment[2]
    assert ("cleanup", True) in workspace.calls


@pytest.mark.parametrize(
    "deleted_test",
    [
        "tests/test_api.py",
        "pkg/__tests__/panel.js",
        "src/panel.spec.ts",
        "internal/config_test.go",
        "test_helpers.py",
        "conftest.py",
    ],
)
def test_execute_blocks_test_file_deletions_by_default(tmp_path, deleted_test):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    workspace._changed_files = ["impl.py", deleted_test]
    harness = FakeHarness(on_implement=touch_file(workspace))

    with pytest.raises(ExecutePhaseError, match="deleted test file"):
        run_execute_phase(
            42,
            config_with_tests(),
            github=github,
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert not any(call[0] == "commit_all" for call in workspace.calls)


def test_execute_allows_test_deletions_when_opted_in(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    workspace._changed_files = ["impl.py", "tests/test_api.py"]
    harness = FakeHarness(on_implement=touch_file(workspace))
    config = MachinistConfig.model_validate(
        {
            "tests": {"command": "pytest -q"},
            "limits": {"allow_test_deletions": True},
        }
    )

    run_execute_phase(
        42,
        config,
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
    )

    assert ("mark_ready", 57) in github.calls


def test_execute_permits_non_test_deletions(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    # docs/testing.md and old_module.py are deletions but not test files.
    workspace._changed_files = ["impl.py", "docs/testing.md", "old_module.py"]
    harness = FakeHarness(on_implement=touch_file(workspace))

    run_execute_phase(
        42,
        config_with_tests(),
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
    )

    assert ("mark_ready", 57) in github.calls


def test_execute_rejects_configured_repo_mismatch_before_github_read(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    workspace.repository_target = lambda: ("github.com", "attacker/other")
    config = MachinistConfig.model_validate({"github": {"repo": "x/y"}})

    with pytest.raises(ExecutePhaseError, match="origin does not match configured"):
        run_execute_phase(
            42,
            config,
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert github.calls == []
    assert workspace.calls == []


@pytest.mark.parametrize(
    "unsafe_pr",
    [
        replace(
            make_pr(),
            is_cross_repository=True,
            head_repository="attacker/y",
        ),
        replace(
            make_pr(),
            is_cross_repository=False,
            head_repository="attacker/y",
        ),
    ],
)
def test_execute_ignores_fork_or_mismatched_head_branch_collision(tmp_path, unsafe_pr):
    github = FakeGitHub(prs=[unsafe_pr])
    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(ExecutePhaseError, match="no open PR"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert not any(call[0] == "provision" for call in workspace.calls)


def test_execute_resolves_instructions_from_approved_workspace_not_controller_tree(
    tmp_path,
):
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "AGENTS.md").write_text("controller-only instructions\n")
    workspace = FakeWorkspace(tmp_path)
    workspace.repo_root = controller
    workspace.path.mkdir()
    (workspace.path / "AGENTS.md").write_text("approved branch instructions\n")
    harness = FakeHarness(on_implement=touch_file(workspace))
    claim = FakeClaim(tmp_path)
    config = MachinistConfig.model_validate(
        {"instructions": {"execute": {"paths": ["AGENTS.md"]}}}
    )

    run_execute_phase(
        42,
        config,
        github=FakeGitHub(prs=[make_pr()]),
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
    )

    assert "approved branch instructions" in harness.prompts[0]
    assert "controller-only instructions" not in harness.prompts[0]
    expected = "approved branch instructions\n"
    assert (
        claim.evidence["instruction_sha256"]
        == hashlib.sha256(expected.encode()).hexdigest()
    )
    assert claim.evidence["instruction_sources"] == ["AGENTS.md"]


def test_claimed_run_uses_fresh_attempt_path_and_captures_harness_report(tmp_path):
    old_workspace = tmp_path / "old-attempt"
    old_workspace.mkdir()
    evidence = old_workspace / "diagnostic.txt"
    evidence.write_text("preserve me\n")
    claim = FakeClaim(
        tmp_path,
        attempt=2,
        previous_evidence={
            "approved_sha": "a" * 40,
            "workspace_path": str(old_workspace),
            "harness_completed": True,
        },
    )
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(
        on_implement=touch_file(workspace),
        report="implemented dark mode\n",
    )

    run_execute_phase(
        42,
        MachinistConfig(),
        github=FakeGitHub(prs=[make_pr()]),
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
    )

    assert (
        "provision",
        "issue-42",
        "agent/issue-42",
        "origin/main",
        2,
    ) in workspace.calls
    assert harness.prompts
    assert evidence.read_text() == "preserve me\n"
    assert str(old_workspace) in claim.evidence["prior_workspace_paths"]
    report_path = Path(claim.evidence["harness_report_path"])
    assert report_path.read_text() == "implemented dark mode\n"
    assert claim.evidence["harness_report_excerpt"] == "implemented dark mode\n"
    assert claim.evidence["harness"] == {
        "name": "fake",
        "model": None,
        "profile": "execute",
    }


def test_real_task_claim_persists_execute_checkpoints_and_logs(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    lifecycle = TaskLifecycle(tmp_path / "runs")

    pr = lifecycle.run(
        42,
        Phase.EXECUTE,
        lambda claim: run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(
                on_implement=touch_file(workspace),
                report="durable harness report\n",
            ),
            workspace=workspace,
            test_runner=passing_tests,
            claim=claim,
        ),
    )

    record = lifecycle.record(42, Phase.EXECUTE)
    assert pr.number == 57
    assert record is not None
    assert record.status is RunStatus.SUCCEEDED
    assert record.evidence["push_intended_sha"] == "c" * 40
    assert record.evidence["push_observed_sha"] == "c" * 40
    assert record.evidence["ready_observed_sha"] == "c" * 40
    assert record.evidence["verification_report"]["success"] is True
    assert Path(record.evidence["harness_report_path"]).read_text() == (
        "durable harness report\n"
    )
    assert (
        "provision",
        "issue-42",
        "agent/issue-42",
        "origin/main",
    ) in workspace.calls
    assert not any(len(call) == 5 for call in workspace.calls if call[0] == "provision")


def test_feedback_is_checkpointed_and_sent_to_harness(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))
    claim = FakeClaim(tmp_path)

    run_execute_phase(
        42,
        MachinistConfig(),
        github=FakeGitHub(prs=[make_pr()]),
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
        feedback="Preserve backwards compatibility.",
    )

    assert "Preserve backwards compatibility." in harness.prompts[0]
    assert claim.evidence["feedback_supplied"] is True
    assert claim.evidence["feedback_characters"] == 33


def test_explicit_resume_reuses_completed_harness_changes_without_rerunning_it(
    tmp_path,
):
    workspace = FakeWorkspace(tmp_path)
    retained = tmp_path / "retained-attempt-1"
    workspace._prepare_path(retained)
    (retained / "impl.py").write_text("retained code\n")
    workspace._dirty = True
    claim = FakeClaim(
        tmp_path,
        attempt=2,
        previous_evidence={
            "approved_sha": "a" * 40,
            "workspace_path": str(retained),
            "workspace_head": "a" * 40,
            "harness_completed": True,
        },
    )
    harness = FakeHarness(error=AssertionError("completed harness must not rerun"))

    run_execute_phase(
        42,
        MachinistConfig(),
        github=FakeGitHub(prs=[make_pr()]),
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
        recovery="resume",
    )

    assert ("resume", retained, "agent/issue-42", "a" * 40) in workspace.calls
    assert not any(call[0] == "provision" for call in workspace.calls)
    assert harness.prompts == []
    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls


def test_no_open_pr_for_branch_refuses(tmp_path):
    with pytest.raises(ExecutePhaseError, match="machinist spec 42"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[]),
            harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path),
            test_runner=passing_tests,
        )


def test_unapproved_pr_refuses_and_names_the_label(tmp_path):
    github = FakeGitHub(prs=[make_pr(labels=())])

    with pytest.raises(ExecutePhaseError, match="machinist:approved"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path),
            test_runner=passing_tests,
        )

    assert not any(c[0] == "mark_ready" for c in github.calls)


def test_missing_approval_sha_refuses(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    github.approval_sha = lambda number: None

    with pytest.raises(ExecutePhaseError, match="approval evidence"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path),
            test_runner=passing_tests,
        )


def test_stale_approval_sha_refuses(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    github.approval_sha = lambda number: "b" * 40

    with pytest.raises(ExecutePhaseError, match="changed after approval"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path),
            test_runner=passing_tests,
        )


def test_provisioned_head_must_still_equal_approved_sha_before_harness_runs(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    workspace._head_override = "b" * 40
    harness = FakeHarness(on_implement=touch_file(workspace))

    with pytest.raises(ExecutePhaseError, match="approved SHA"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert harness.prompts == []
    assert not any(call[0] == "push" for call in workspace.calls)
    assert ("cleanup", False) in workspace.calls


def test_remote_head_must_still_equal_approved_sha_before_harness_runs(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    workspace._remote_sha = "b" * 40
    harness = FakeHarness(on_implement=touch_file(workspace))

    with pytest.raises(ExecutePhaseError, match="remote.*approved SHA"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert harness.prompts == []
    assert not any(call[0] == "push" for call in workspace.calls)


def test_already_implemented_pr_refuses_without_force(tmp_path):
    github = FakeGitHub(prs=[make_pr(draft=False)])
    harness = FakeHarness()

    with pytest.raises(ExecutePhaseError, match="--force"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=harness,
            workspace=FakeWorkspace(tmp_path),
            test_runner=passing_tests,
        )

    assert harness.prompts == []


def test_force_reimplements_a_ready_pr(tmp_path):
    github = FakeGitHub(prs=[make_pr(draft=False)])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    pr = run_execute_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
        force=True,
    )

    assert pr.number == 57
    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls


def test_missing_spec_file_fails_before_harness_runs(tmp_path):
    harness = FakeHarness()
    workspace = FakeWorkspace(tmp_path, spec_text=None)

    with pytest.raises(ExecutePhaseError, match="spec"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert harness.prompts == []
    assert ("cleanup", False) in workspace.calls


@pytest.mark.parametrize("trap", ["leaf", "parent"])
def test_execute_rejects_spec_symlink_without_disclosing_external_text(tmp_path, trap):
    outside = tmp_path / "outside"
    outside.mkdir()
    external_spec = outside / "issue-42-spec.md"
    external_spec.write_text("TOP-SECRET\n")

    class TrappedWorkspace(FakeWorkspace):
        def _prepare_path(self, path):
            path.mkdir(parents=True, exist_ok=True)
            if trap == "leaf":
                spec_dir = path / ".machinist/specs"
                spec_dir.mkdir(parents=True, exist_ok=True)
                (spec_dir / "issue-42-spec.md").symlink_to(external_spec)
            else:
                external_specs = outside / "specs"
                external_specs.mkdir(exist_ok=True)
                target = external_specs / external_spec.name
                if external_spec.exists():
                    external_spec.rename(target)
                (path / ".machinist").symlink_to(outside, target_is_directory=True)

    workspace = TrappedWorkspace(tmp_path)
    harness = FakeHarness()

    with pytest.raises(ExecutePhaseError, match="cannot safely read approved Spec"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    protected = (
        external_spec if trap == "leaf" else outside / "specs" / "issue-42-spec.md"
    )
    assert protected.read_text() == "TOP-SECRET\n"
    assert harness.prompts == []
    assert ("cleanup", False) in workspace.calls


def test_execute_rejects_approved_spec_over_configured_size_limit(tmp_path):
    workspace = FakeWorkspace(tmp_path, spec_text="123456")
    harness = FakeHarness()
    config = MachinistConfig.model_validate({"limits": {"max_spec_chars": 5}})

    with pytest.raises(ExecutePhaseError, match="6 characters; maximum is 5"):
        run_execute_phase(
            42,
            config,
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert harness.prompts == []
    assert ("cleanup", False) in workspace.calls


def test_execute_bounds_spec_bytes_before_decoding_entire_file(tmp_path):
    workspace = FakeWorkspace(tmp_path, spec_text="x" * 21)
    harness = FakeHarness()
    config = MachinistConfig.model_validate({"limits": {"max_spec_chars": 5}})

    with pytest.raises(ExecutePhaseError, match="20-byte limit"):
        run_execute_phase(
            42,
            config,
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert harness.prompts == []


def test_execute_rejects_non_utf8_approved_spec(tmp_path):
    class InvalidSpecWorkspace(FakeWorkspace):
        def _prepare_path(self, path):
            spec_dir = path / ".machinist/specs"
            spec_dir.mkdir(parents=True, exist_ok=True)
            (spec_dir / "issue-42-spec.md").write_bytes(b"\xff\xfe")

    workspace = InvalidSpecWorkspace(tmp_path)
    harness = FakeHarness()

    with pytest.raises(ExecutePhaseError, match="not valid UTF-8"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert harness.prompts == []


def test_no_changes_from_harness_fails(tmp_path):
    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(ExecutePhaseError, match="no changes"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert not any(c[0] == "push" for c in workspace.calls)


@pytest.mark.parametrize("violation", ["commit", "push", "machinist"])
def test_harness_git_and_pipeline_violations_are_rejected(tmp_path, violation):
    workspace = FakeWorkspace(tmp_path)

    def violate(cwd):
        workspace._dirty = True
        if violation == "commit":
            workspace._head_override = "d" * 40
        elif violation == "push":
            workspace._remote_sha = "e" * 40
        else:
            workspace._machinist_changed = True

    with pytest.raises(
        ExecutePhaseError, match=r"custody|harness|machinist|\.machinist"
    ):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=violate),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert not any(call[0] == "commit_all" for call in workspace.calls)


@pytest.mark.parametrize("attack", ["hook", "origin"])
def test_real_execute_harness_cannot_seize_git_authority_or_leak_token(
    tmp_path, monkeypatch, attack
):
    controller, origin, approved_sha = real_approved_execute_repo(tmp_path)
    config = MachinistConfig.model_validate(
        {"workspace": {"root": str(tmp_path / "workspaces")}}
    )
    workspace = Workspace(controller, config.workspace)
    workspace.repository_target = lambda: ("github.com", "x/y")
    approved_pr = replace(make_pr(), head_sha=approved_sha)
    github = FakeGitHub(prs=[approved_pr])
    github.approval_sha = lambda _number: approved_sha
    leak = tmp_path / "controller-secret-leak.txt"
    attacker = tmp_path / "attacker.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(attacker)],
        capture_output=True,
        check=True,
    )
    monkeypatch.setenv("GH_TOKEN", "controller-secret")

    def malicious_harness(cwd):
        (cwd / "implementation.py").write_text("implemented = True\n")
        if attack == "hook":
            common = Path(real_git(cwd, "rev-parse", "--git-common-dir"))
            if not common.is_absolute():
                common = (cwd / common).resolve()
            hook = common / "hooks" / "pre-commit"
            hook.write_text(f"#!/bin/sh\nprintf '%s' \"$GH_TOKEN\" > {leak}\n")
            hook.chmod(0o755)
        else:
            real_git(cwd, "remote", "set-url", "origin", str(attacker))

    with pytest.raises(ExecutePhaseError, match="controller-owned Git metadata"):
        run_execute_phase(
            42,
            config,
            github=github,
            harness=FakeHarness(on_implement=malicious_harness),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert not leak.exists()
    assert real_git(origin, "rev-parse", "refs/heads/agent/issue-42") == approved_sha
    attacker_branch = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/agent/issue-42"],
        cwd=attacker,
        capture_output=True,
    )
    assert attacker_branch.returncode != 0


def test_failing_test_gate_keeps_workspace_and_never_pushes(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    with pytest.raises(ExecutePhaseError, match="test_x"):
        run_execute_phase(
            42,
            config_with_tests(),
            github=github,
            harness=harness,
            workspace=workspace,
            test_runner=failing_tests,
        )

    assert not any(c[0] in ("commit_all", "push") for c in workspace.calls)
    assert not any(c[0] == "mark_ready" for c in github.calls)
    assert ("cleanup", False) in workspace.calls


def test_named_verification_report_is_checkpointed_and_in_completion_comment(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))
    claim = FakeClaim(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs["timeout"]))
        Path(kwargs["stdout_log"]).write_text("lint passed\n")
        Path(kwargs["stderr_log"]).write_text("")
        return subprocess.CompletedProcess(command, 0, "lint passed", "")

    run_execute_phase(
        42,
        config_with_named_gate(),
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=runner,
        claim=claim,
    )

    assert calls == [("lint", 120)]
    assert claim.evidence["verification_report"]["success"] is True
    assert claim.evidence["verification_report"]["gates"][0]["name"] == "lint"
    comment = next(call for call in github.calls if call[0] == "upsert_pr_comment")
    assert "lint" in comment[2]
    assert "passed" in comment[2]


def test_advisory_forbidden_mutation_is_checkpointed_and_never_committed(tmp_path):
    github = FakeGitHub(prs=[make_pr()])
    workspace = FakeWorkspace(tmp_path)
    claim = FakeClaim(tmp_path)
    config = MachinistConfig.model_validate(
        {
            "verification": {
                "gates": [
                    {
                        "name": "advisory audit",
                        "command": "audit",
                        "required": False,
                        "mutation_policy": "forbid",
                    }
                ]
            }
        }
    )

    def mutating_runner(command, **_kwargs):
        workspace._snapshot = "mutated"
        return subprocess.CompletedProcess(command, 0, "audit passed", "")

    with pytest.raises(ExecutePhaseError, match="mutation_detected"):
        run_execute_phase(
            42,
            config,
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=mutating_runner,
            claim=claim,
        )

    report = claim.evidence["verification_report"]
    assert report["success"] is False
    assert report["blocking_failures"] == ["advisory audit"]
    assert report["required_failures"] == []
    assert report["advisory_failures"] == ["advisory audit"]
    assert report["gates"][0]["blocking"] is True
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert not any(call[0] == "mark_ready" for call in github.calls)
    assert ("cleanup", False) in workspace.calls


def test_verification_cancellation_is_checkpointed_and_typed(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    claim = FakeClaim(tmp_path)

    def cancel_check():
        return True

    def cancelled_runner(command, **kwargs):
        assert kwargs["cancel_check"] is cancel_check
        raise ProcessCancelledError(command, stdout="stopping", stderr="cancelled")

    with pytest.raises(ExecutePhaseCancelled) as caught:
        run_execute_phase(
            42,
            config_with_named_gate(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=cancelled_runner,
            claim=claim,
            cancel_check=cancel_check,
        )

    assert caught.value.cancelled is True
    assert claim.evidence["verification_report"]["gates"][0]["status"] == "cancelled"
    assert ("cleanup", False) in workspace.calls


def test_cancellation_racing_after_verification_prevents_all_delivery(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    github = FakeGitHub(prs=[make_pr()])
    cancelled = False

    def cancel_check():
        return cancelled

    def passing_then_cancel(command, **_kwargs):
        nonlocal cancelled
        cancelled = True
        return subprocess.CompletedProcess(command, 0, "passed", "")

    with pytest.raises(ExecutePhaseCancelled, match="before commit") as caught:
        run_execute_phase(
            42,
            config_with_named_gate(),
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_then_cancel,
            cancel_check=cancel_check,
        )

    assert caught.value.cancelled is True
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert not any(
        call[0] in {"upsert_pr_comment", "mark_ready"} for call in github.calls
    )
    assert ("cleanup", False) in workspace.calls


def test_advisory_verification_straggler_is_checkpointed_and_never_committed(
    tmp_path,
):
    workspace = FakeWorkspace(tmp_path)
    claim = FakeClaim(tmp_path)
    config = MachinistConfig.model_validate(
        {
            "verification": {
                "gates": [
                    {
                        "name": "advisory helper check",
                        "command": "leaky-check",
                        "required": False,
                        "mutation_policy": "forbid",
                    }
                ]
            }
        }
    )

    def straggling_runner(command, **_kwargs):
        raise ProcessStragglerError(
            command,
            0,
            stdout="leader completed",
            stderr="background helper remained",
        )

    with pytest.raises(ExecutePhaseError, match="straggler"):
        run_execute_phase(
            42,
            config,
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=straggling_runner,
            claim=claim,
        )

    report = claim.evidence["verification_report"]
    assert report["success"] is False
    assert report["blocking_failures"] == ["advisory helper check"]
    assert report["gates"][0]["status"] == "straggler"
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert ("cleanup", False) in workspace.calls


def test_change_file_limit_blocks_commit_and_push(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    workspace._changed_files = ["one.py", "two.py"]
    harness = FakeHarness(on_implement=touch_file(workspace))
    config = MachinistConfig.model_validate({"limits": {"max_changed_files": 1}})

    with pytest.raises(ExecutePhaseError, match="changed 2 files"):
        run_execute_phase(
            42,
            config,
            github=FakeGitHub(prs=[make_pr()]),
            harness=harness,
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)


def test_pipeline_metadata_is_denied_even_if_custom_denied_paths_are_empty(tmp_path):
    workspace = FakeWorkspace(tmp_path)

    def change_pipeline(cwd):
        workspace._dirty = True
        workspace._machinist_changed = True

    config = MachinistConfig.model_validate({"limits": {"denied_paths": []}})
    with pytest.raises(ExecutePhaseError, match="controller-owned"):
        run_execute_phase(
            42,
            config,
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=change_pipeline),
            workspace=workspace,
            test_runner=passing_tests,
        )


def test_change_byte_and_binary_limits_fail_closed(tmp_path):
    workspace = FakeWorkspace(tmp_path)

    def write_binary(cwd):
        (cwd / "impl.py").write_bytes(b"abc\x00def")
        workspace._dirty = True

    with pytest.raises(ExecutePhaseError, match="binary file"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=write_binary),
            workspace=workspace,
            test_runner=passing_tests,
        )

    workspace = FakeWorkspace(tmp_path / "bytes")
    config = MachinistConfig.model_validate({"limits": {"max_changed_bytes": 4}})
    with pytest.raises(ExecutePhaseError, match="changed content is too large"):
        run_execute_phase(
            42,
            config,
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
        )


def test_null_test_command_skips_the_gate(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_implement=touch_file(workspace))

    def exploding_runner(*args, **kwargs):
        raise AssertionError("test gate should not run")

    pr = run_execute_phase(
        42,
        MachinistConfig(),  # tests.command is null by default
        github=FakeGitHub(prs=[make_pr()]),
        harness=harness,
        workspace=workspace,
        test_runner=exploding_runner,
    )

    assert pr.number == 57
    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls


def test_mutation_allowed_gate_cannot_erase_the_entire_implementation(tmp_path):
    workspace = FakeWorkspace(tmp_path)

    def erase_changes(command, **kwargs):
        workspace._dirty = False
        workspace._changed_files = []
        return subprocess.CompletedProcess(command, 0, "formatted", "")

    with pytest.raises(ExecutePhaseError, match="removed all implementation changes"):
        run_execute_phase(
            42,
            config_with_tests("formatter"),
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=erase_changes,
        )

    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)


def test_partial_push_retry_marks_ready_without_rerunning_harness(tmp_path):
    recovered = make_pr()
    object.__setattr__(recovered, "head_sha", "c" * 40)
    github = FakeGitHub(prs=[recovered])
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(error=AssertionError("harness must not rerun"))
    claim = FakeClaim(
        tmp_path,
        attempt=2,
        previous_evidence={
            "approved_sha": "a" * 40,
            "implementation_sha": "c" * 40,
            "push_intended_sha": "c" * 40,
            "change_summary": {"files": ["impl.py"], "file_count": 1, "bytes": 5},
            "verification_report": {"success": True, "gates": []},
        },
    )

    pr = run_execute_phase(
        42,
        config_with_tests(),
        github=github,
        harness=harness,
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
    )

    assert pr.number == 57
    assert harness.prompts == []
    assert not any(call[0] == "provision" for call in workspace.calls)
    assert any(call[0] == "upsert_pr_comment" for call in github.calls)
    assert ("mark_ready", 57) in github.calls


def test_stale_pr_read_does_not_erase_unobserved_push_intent(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    # GitHub's PR list still reports the approved head, while the remote read
    # already observes the implementation pushed by the interrupted attempt.
    workspace._remote_sha = "c" * 40
    claim = FakeClaim(
        tmp_path,
        attempt=2,
        previous_evidence={
            "approved_sha": "a" * 40,
            "implementation_sha": "c" * 40,
            "push_intended_sha": "c" * 40,
            "workspace_path": str(tmp_path / "attempt-1"),
        },
    )

    with pytest.raises(ExecutePhaseError, match="remote.*approved SHA"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(prs=[make_pr()]),
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
            claim=claim,
        )

    assert claim.evidence["push_intended_sha"] == "c" * 40
    assert claim.evidence["implementation_sha"] == "c" * 40
    assert claim.evidence["workspace_path"] == str(workspace.path)


def test_push_intent_is_durable_before_push_and_observation_follows(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    claim = FakeClaim(tmp_path)

    def inspect_checkpoint():
        assert claim.evidence["approved_sha"] == "a" * 40
        assert claim.evidence["implementation_sha"] == "c" * 40
        assert claim.evidence["push_intended_sha"] == "c" * 40
        assert claim.evidence.get("push_observed_sha") is None

    workspace.on_push = inspect_checkpoint

    run_execute_phase(
        42,
        MachinistConfig(),
        github=FakeGitHub(prs=[make_pr()]),
        harness=FakeHarness(on_implement=touch_file(workspace)),
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
    )

    assert claim.evidence["push_observed_sha"] == "c" * 40


def test_delivery_refuses_when_github_pr_head_does_not_observe_pushed_sha(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    github = FakeGitHub(prs=[make_pr()])
    github.delivery_head_sha = "a" * 40

    with pytest.raises(ExecutePhaseError, match="GitHub PR head does not match"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls
    assert workspace._remote_sha == "c" * 40
    assert not any(
        call[0] in {"upsert_pr_comment", "mark_ready"} for call in github.calls
    )
    assert ("cleanup", False) in workspace.calls


def test_delivery_refuses_cross_repository_pr_observed_after_push(tmp_path):
    class ForkDeliveryGitHub(FakeGitHub):
        def pr_for_branch(self, branch):
            current = super().pr_for_branch(branch)
            return replace(
                current,
                is_cross_repository=True,
                head_repository="attacker/y",
            )

    workspace = FakeWorkspace(tmp_path)
    github = ForkDeliveryGitHub(prs=[make_pr()])

    with pytest.raises(ExecutePhaseError, match="identity/state changed"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls
    assert not any(
        call[0] in {"upsert_pr_comment", "mark_ready"} for call in github.calls
    )


def test_delivery_refuses_pr_retargeted_after_implementation_push(tmp_path):
    class RetargetedDeliveryGitHub(FakeGitHub):
        def pr_for_branch(self, branch):
            current = super().pr_for_branch(branch)
            return replace(current, base="release")

    workspace = FakeWorkspace(tmp_path)
    github = RetargetedDeliveryGitHub(prs=[make_pr()])

    with pytest.raises(ExecutePhaseError, match="identity/state changed"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
        )

    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls
    assert not any(
        call[0] in {"upsert_pr_comment", "mark_ready"} for call in github.calls
    )


def test_delivery_refuses_to_checkpoint_when_mark_ready_is_not_observed(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    github = FakeGitHub(prs=[make_pr()])
    claim = FakeClaim(tmp_path)

    def no_op_mark_ready(number):
        github.calls.append(("mark_ready", number))

    github.mark_ready = no_op_mark_ready

    with pytest.raises(ExecutePhaseError, match="remained a draft"):
        run_execute_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(on_implement=touch_file(workspace)),
            workspace=workspace,
            test_runner=passing_tests,
            claim=claim,
        )

    assert ("mark_ready", 57) in github.calls
    assert claim.evidence["ready_observed_sha"] is None
    assert ("cleanup", False) in workspace.calls


def test_observed_ready_delivery_wins_over_cancel_racing_with_mark_ready(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    github = FakeGitHub(prs=[make_pr()])
    claim = FakeClaim(tmp_path)
    cancelled = False
    original_mark_ready = github.mark_ready

    def mark_ready_then_cancel(number):
        nonlocal cancelled
        original_mark_ready(number)
        cancelled = True

    github.mark_ready = mark_ready_then_cancel

    pr = run_execute_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(on_implement=touch_file(workspace)),
        workspace=workspace,
        test_runner=passing_tests,
        claim=claim,
        cancel_check=lambda: cancelled,
    )

    assert pr.number == 57
    assert ("mark_ready", 57) in github.calls
    assert claim.evidence["ready_observed_sha"] == "c" * 40
    assert ("cleanup", True) in workspace.calls


def test_resume_after_unobserved_push_reuses_committed_workspace(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    workspace._head_override = "c" * 40
    retained = tmp_path / "retained-committed"
    workspace._prepare_path(retained)
    claim = FakeClaim(
        tmp_path,
        attempt=2,
        previous_evidence={
            "approved_sha": "a" * 40,
            "implementation_sha": "c" * 40,
            "push_intended_sha": "c" * 40,
            "workspace_path": str(retained),
            "workspace_head": "c" * 40,
            "harness_completed": True,
            "change_summary": {"files": ["impl.py"], "file_count": 1, "bytes": 5},
            "verification_report": {"success": True, "gates": []},
        },
    )
    harness = FakeHarness(error=AssertionError("harness must not rerun"))

    run_execute_phase(
        42,
        config_with_tests(),
        github=FakeGitHub(prs=[make_pr()]),
        harness=harness,
        workspace=workspace,
        test_runner=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("verification must not rerun")
        ),
        claim=claim,
        recovery="resume",
    )

    assert ("resume", retained, "agent/issue-42", "c" * 40) in workspace.calls
    assert ("push", "agent/issue-42", "a" * 40) in workspace.calls
    assert harness.prompts == []

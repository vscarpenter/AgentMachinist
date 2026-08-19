"""Tests for Phase 3 orchestration: approved spec → implementation → ready PR."""

import subprocess
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
from machinist.process import ProcessCancelledError


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
        self._next_comment_id = 123

    def open_machinist_prs(self, prefix):
        self.calls.append(("open_machinist_prs", prefix))
        return self._prs

    def default_branch(self):
        return "main"

    def approval_sha(self, number):
        return "a" * 40

    def mark_ready(self, number):
        self.calls.append(("mark_ready", number))

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
    comment = next(call for call in github.calls if call[0] == "upsert_pr_comment")
    assert "Implementation complete" in comment[2]
    assert "c" * 12 in comment[2]
    assert "Effective harness: `fake`" in comment[2]
    assert "done" in comment[2]
    assert ("cleanup", True) in workspace.calls


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

"""Tests for Phase 1 orchestration: issue → spec → branch → draft PR."""

import hashlib
import shutil
from dataclasses import replace

import pytest

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue, PullRequest
from machinist.lifecycle import Phase, RunStatus, TaskLifecycle
from machinist.phases.spec import (
    SpecPhaseCancelled,
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
        self.labels = {**getattr(self, "labels", {}), name: (color, description)}

    def create_draft_pr(self, *, branch, base, title, body):
        self.calls.append(("create_draft_pr", branch, base, title, body))
        result = DraftPR(number=57, url="https://github.com/vscarpenter/demo/pull/57")
        self.existing_pr = PullRequest(
            number=result.number,
            title=title,
            url=result.url,
            branch=branch,
            is_draft=True,
            head_sha="b" * 40,
            base=base,
        )
        return result

    def pr_for_branch(self, branch):
        self.calls.append(("pr_for_branch", branch))
        return self.existing_pr

    def mark_draft(self, number):
        self.calls.append(("mark_draft", number))

    def reopen_pr(self, number):
        self.calls.append(("reopen_pr", number))
        self.existing_pr = replace(self.existing_pr, state="OPEN")

    def update_pr(self, number, *, title=None, body=None):
        self.calls.append(("update_pr", number, title, body))
        self.existing_pr = replace(
            self.existing_pr,
            title=title or self.existing_pr.title,
            head_sha="b" * 40,
        )

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
        self._remote_sha = None

    def repository_identity(self):
        return "vscarpenter/demo"

    def repository_target(self):
        return "github.com", "vscarpenter/demo"

    def provision(self, task, branch, base_ref, *, attempt=None):
        self.calls.append(("provision", task, branch, base_ref))
        if attempt is not None:
            self.path = self.path.parent / f"ws-attempt-{attempt}"
        self.path.mkdir(parents=True)
        return self.path

    def provision_preview(self, task, branch, base_ref):
        self.calls.append(("provision_preview", task, branch, base_ref))
        self.path = self.path.parent / task
        self.path.mkdir(parents=True)
        return self.path

    def cleanup_preview(self, path):
        self.calls.append(("cleanup_preview", path))
        if path.exists():
            shutil.rmtree(path)

    def commit_all(self, path, message):
        self.calls.append(("commit_all", message))

    def push(self, path, branch, expected_sha=None):
        call = (
            ("push", branch) if expected_sha is None else ("push", branch, expected_sha)
        )
        self.calls.append(call)
        self._remote_sha = self.head_sha(path)

    def remote_sha(self, path, branch):
        return self._remote_sha

    def has_changes(self, path):
        return self.dirty

    def head_sha(self, path):
        return "b" * 40

    def cleanup(self, path, *, success):
        self.calls.append(("cleanup", success))


class FakeClaim:
    attempt = 1

    def __init__(self, previous=None):
        self.previous_evidence = dict(previous or {})
        self.evidence = dict(self.previous_evidence)
        self.progress_calls = []

    def progress(self, stage, detail=None):
        self.progress_calls.append((stage, detail))

    def checkpoint(self, **evidence):
        self.evidence.update(evidence)


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


def test_render_spec_prompt_requires_risks_and_standalone_quality_bar():
    prompt = render_spec_prompt(ISSUE)

    # The spec is the approval-gate artifact: it must surface risks and be
    # implementable by someone who never saw the originating conversation.
    assert "## Risks" in prompt
    assert prompt.index("## Proposed approach") < prompt.index("## Risks")
    assert prompt.index("## Risks") < prompt.index("## Testing plan")
    assert "unfamiliar with this conversation" in prompt


def test_happy_path_creates_spec_branch_and_draft_pr(tmp_path):
    github, harness = FakeGitHub(), FakeHarness()
    workspace = FakeWorkspace(tmp_path)
    config = MachinistConfig()

    pr = run_spec_phase(
        42,
        config,
        github=github,
        harness=harness,
        workspace=workspace,
        claim=FakeClaim(),
    )

    assert pr.number == 57
    assert ("provision", "issue-42", "agent/issue-42", "origin/main") in workspace.calls

    spec_file = workspace.path / ".machinist" / "specs" / "issue-42-spec.md"
    assert spec_file.read_text() == harness.spec_text

    commit = next(c for c in workspace.calls if c[0] == "commit_all")
    assert "#42" in commit[1]
    assert ("push", "agent/issue-42") in workspace.calls
    assert ("ensure_label", "machinist:approved") in github.calls
    assert github.repo == "vscarpenter/demo"
    assert github.repo_host == "github.com"
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
    assert f"/machinist-execute {'b' * 40}" in body
    # Dogfood UX finding: solo devs reach for GitHub's review Approve button,
    # which GitHub blocks on their own PRs. The body must head that off.
    assert "Approve button" in body
    # Dogfood UX finding 2 (both live lifecycles): users instinctively click
    # "Ready for review", which blocks the daemon (it implements drafts only).
    assert "leave this PR as a draft" in body


def test_cleanup_failure_after_observed_spec_delivery_is_a_success_warning(tmp_path):
    class CleanupFailingWorkspace(FakeWorkspace):
        def cleanup(self, path, *, success):
            self.calls.append(("cleanup", success))
            raise OSError("Workshop is busy")

    lifecycle = TaskLifecycle(tmp_path / "runs")
    workspace = CleanupFailingWorkspace(tmp_path)

    pr = lifecycle.run(
        42,
        Phase.SPEC,
        lambda claim: run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=FakeHarness(),
            workspace=workspace,
            claim=claim,
        ),
    )

    record = lifecycle.record(42, Phase.SPEC)
    assert pr.number == 57
    assert record.status is RunStatus.SUCCEEDED
    assert record.evidence["cleanup_succeeded"] is False
    assert record.evidence["harness"] == {
        "name": "fake",
        "model": None,
        "profile": "spec",
        "structured_usage": False,
    }
    assert "Workshop is busy" in record.evidence["cleanup_warning"]
    assert record.evidence["retained_workspace_path"] == str(workspace.path)


@pytest.mark.parametrize("trap", ["leaf", "parent"])
def test_spec_rejects_symlink_trap_without_clobbering_external_file(tmp_path, trap):
    outside = tmp_path / "outside"
    outside.mkdir()
    external_spec = outside / "issue-42-spec.md"
    external_spec.write_text("KEEP\n")

    class TrappedWorkspace(FakeWorkspace):
        def provision(self, task, branch, base_ref, *, attempt=None):
            path = super().provision(task, branch, base_ref, attempt=attempt)
            if trap == "leaf":
                spec_dir = path / ".machinist/specs"
                spec_dir.mkdir(parents=True)
                (spec_dir / "issue-42-spec.md").symlink_to(external_spec)
            else:
                external_specs = outside / "specs"
                external_specs.mkdir()
                external_spec.rename(external_specs / external_spec.name)
                (path / ".machinist").symlink_to(outside, target_is_directory=True)
            return path

    workspace = TrappedWorkspace(tmp_path)
    harness = FakeHarness(spec_text="CLOBBER\n")

    with pytest.raises(SpecPhaseError, match="cannot safely write Spec"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=harness,
            workspace=workspace,
            claim=FakeClaim(),
        )

    protected = (
        external_spec if trap == "leaf" else outside / "specs" / "issue-42-spec.md"
    )
    assert protected.read_text() == "KEEP\n"
    assert ("cleanup", False) in workspace.calls
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)


def test_spec_rejects_configured_repo_mismatch_before_github_read(tmp_path):
    github = FakeGitHub()
    workspace = FakeWorkspace(tmp_path)
    workspace.repository_target = lambda: ("github.com", "attacker/other")
    config = MachinistConfig.model_validate({"github": {"repo": "vscarpenter/demo"}})

    with pytest.raises(SpecPhaseError, match="origin does not match configured"):
        run_spec_phase(
            42,
            config,
            github=github,
            harness=FakeHarness(),
            workspace=workspace,
            claim=FakeClaim(),
        )

    assert github.calls == []
    assert workspace.calls == []


@pytest.mark.parametrize(
    "unsafe_pr",
    [
        PullRequest(
            number=57,
            title="Fork collision",
            url="https://github.com/vscarpenter/demo/pull/57",
            branch="agent/issue-42",
            is_draft=True,
            head_sha="a" * 40,
            is_cross_repository=True,
            head_repository="attacker/demo",
        ),
        PullRequest(
            number=57,
            title="Mismatched head",
            url="https://github.com/vscarpenter/demo/pull/57",
            branch="agent/issue-42",
            is_draft=True,
            head_sha="a" * 40,
            head_repository="attacker/demo",
        ),
    ],
)
def test_spec_refuses_fork_or_mismatched_existing_pr_custody(tmp_path, unsafe_pr):
    github = FakeGitHub(existing_pr=unsafe_pr)
    workspace = FakeWorkspace(tmp_path)
    workspace._remote_sha = unsafe_pr.head_sha

    with pytest.raises(SpecPhaseError, match="cross-repository|head repository"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=workspace,
            revise=True,
            claim=FakeClaim(),
        )

    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert not any(
        call[0] in {"ensure_label", "update_pr", "reopen_pr"} for call in github.calls
    )


def test_branch_prefix_comes_from_config(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    config = MachinistConfig.model_validate({"workspace": {"branch_prefix": "bot/"}})

    run_spec_phase(
        42,
        config,
        github=FakeGitHub(),
        harness=FakeHarness(),
        workspace=workspace,
        claim=FakeClaim(),
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
    provision = next(call for call in workspace.calls if call[0] == "provision_preview")
    assert provision[1].startswith("preview-issue-42-")
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert not any(
        call[0] in {"ensure_label", "create_draft_pr", "update_pr"}
        for call in github.calls
    )
    assert any(call[0] == "cleanup_preview" for call in workspace.calls)
    assert not workspace.path.exists()


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
    workspace._remote_sha = existing.head_sha

    result = run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(spec_text="## Revised\nDo the safer thing.\n"),
        workspace=workspace,
        revise=True,
        claim=FakeClaim(),
    )

    assert (result.number, result.url) == (57, existing.url)
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
    workspace = FakeWorkspace(tmp_path)
    workspace._remote_sha = existing.head_sha

    run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(),
        workspace=workspace,
        revise=True,
        claim=FakeClaim(),
    )

    assert ("reopen_pr", 57) in github.calls


def test_revise_fails_if_mark_draft_does_not_take_effect(tmp_path):
    class NoopMarkDraftGitHub(FakeGitHub):
        def mark_draft(self, number):
            self.calls.append(("mark_draft", number))

    existing = PullRequest(
        number=57,
        title="Spec: old",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        is_draft=False,
        head_sha="a" * 40,
    )
    github = NoopMarkDraftGitHub(existing_pr=existing)
    workspace = FakeWorkspace(tmp_path)
    workspace._remote_sha = existing.head_sha

    with pytest.raises(SpecPhaseError, match="not a draft"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=workspace,
            revise=True,
            claim=FakeClaim(),
        )

    assert ("mark_draft", 57) in github.calls
    assert ("cleanup", False) in workspace.calls


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
            claim=FakeClaim(),
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
            claim=FakeClaim(),
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
            claim=FakeClaim(),
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
            claim=FakeClaim(),
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
            claim=FakeClaim(),
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
            claim=FakeClaim(),
        )

    assert not any(call[0] == "commit_all" for call in workspace.calls)
    assert ("cleanup", False) in workspace.calls


def test_cancellation_racing_after_harness_never_commits_or_publishes(tmp_path):
    github = FakeGitHub()
    workspace = FakeWorkspace(tmp_path)
    checks = iter((False, False, True))

    with pytest.raises(SpecPhaseCancelled, match="cancelled by operator") as caught:
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=workspace,
            cancel_check=lambda: next(checks),
            claim=FakeClaim(),
        )

    assert caught.value.cancelled is True
    assert not any(call[0] in {"commit_all", "push"} for call in workspace.calls)
    assert not any(
        call[0] in {"ensure_label", "create_draft_pr", "update_pr"}
        for call in github.calls
    )
    assert ("cleanup", False) in workspace.calls


def test_preview_checks_cancellation_after_harness(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    checks = iter((False, False, True))

    with pytest.raises(SpecPhaseCancelled):
        preview_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=FakeHarness(),
            workspace=workspace,
            cancel_check=lambda: next(checks),
        )

    assert any(call[0] == "cleanup_preview" for call in workspace.calls)
    assert not workspace.path.exists()


def test_normal_run_refuses_to_hijack_existing_branch_pr(tmp_path):
    existing = PullRequest(
        number=99,
        title="Unrelated work",
        url="https://github.com/x/y/pull/99",
        branch="agent/issue-42",
        is_draft=True,
        head_sha="a" * 40,
    )
    workspace = FakeWorkspace(tmp_path)
    workspace._remote_sha = existing.head_sha
    github = FakeGitHub(existing_pr=existing)
    harness = FakeHarness()

    with pytest.raises(SpecPhaseError, match="refusing to mutate"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=harness,
            workspace=workspace,
            claim=FakeClaim(),
        )

    assert harness.prompts == []
    assert not any(call[0] == "update_pr" for call in github.calls)


def test_pr_created_crash_is_reconciled_by_checkpointed_retry(tmp_path):
    class CrashOnceAfterCreateGitHub(FakeGitHub):
        def __init__(self):
            super().__init__()
            self.crashed = False

        def pr_for_branch(self, branch):
            self.calls.append(("pr_for_branch", branch))
            if self.existing_pr is not None and not self.crashed:
                self.crashed = True
                raise RuntimeError("controller crashed after PR creation")
            return self.existing_pr

    github = CrashOnceAfterCreateGitHub()
    first_workspace = FakeWorkspace(tmp_path / "first")
    first_claim = FakeClaim()

    with pytest.raises(RuntimeError, match="after PR creation"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=first_workspace,
            claim=first_claim,
        )

    assert first_claim.evidence["pr_number"] == 57
    assert first_claim.evidence["push_observed_sha"] == "b" * 40

    retry_workspace = FakeWorkspace(tmp_path / "retry")
    retry_workspace._remote_sha = first_workspace._remote_sha
    retry_claim = FakeClaim(first_claim.evidence)
    result = run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(
            error=AssertionError("delivery-only retry must not regenerate")
        ),
        workspace=retry_workspace,
        claim=retry_claim,
    )

    assert result.number == 57
    assert len([call for call in github.calls if call[0] == "create_draft_pr"]) == 1
    assert any(call[0] == "update_pr" for call in github.calls)
    assert not any(call[0] in {"commit_all", "push"} for call in retry_workspace.calls)
    assert "spec_recovery" not in retry_claim.evidence


def test_label_failure_after_push_retries_as_delivery_only_and_creates_pr(tmp_path):
    class FailLabelOnceGitHub(FakeGitHub):
        def __init__(self):
            super().__init__()
            self.failed = False

        def ensure_label(self, name, *, color, description):
            self.calls.append(("ensure_label", name))
            if not self.failed:
                self.failed = True
                raise RuntimeError("label API unavailable after push")

    github = FailLabelOnceGitHub()
    first_workspace = FakeWorkspace(tmp_path / "first")
    first_claim = FakeClaim()

    with pytest.raises(RuntimeError, match="label API unavailable"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=first_workspace,
            claim=first_claim,
        )

    assert first_claim.evidence["push_observed_sha"] == "b" * 40
    assert github.existing_pr is None

    retry_workspace = FakeWorkspace(tmp_path / "retry")
    retry_workspace._remote_sha = first_workspace._remote_sha
    retry_claim = FakeClaim(first_claim.evidence)
    result = run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(
            error=AssertionError("delivery-only retry must not regenerate")
        ),
        workspace=retry_workspace,
        claim=retry_claim,
    )

    assert result.number == 57
    assert len([call for call in github.calls if call[0] == "create_draft_pr"]) == 1
    assert not any(call[0] in {"commit_all", "push"} for call in retry_workspace.calls)
    assert "spec_recovery" not in retry_claim.evidence


def test_remote_push_is_observed_before_checkpoint(tmp_path):
    class StaleRemoteWorkspace(FakeWorkspace):
        def push(self, path, branch, expected_sha=None):
            self.calls.append(("push", branch))

    claim = FakeClaim()

    with pytest.raises(SpecPhaseError, match="remote branch"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=FakeHarness(),
            workspace=StaleRemoteWorkspace(tmp_path),
            claim=claim,
        )

    assert claim.evidence["push_intended_sha"] == "b" * 40
    assert "push_observed_sha" not in claim.evidence


def test_post_delivery_rejects_pr_from_wrong_head(tmp_path):
    class StaleGitHub(FakeGitHub):
        def create_draft_pr(self, **kwargs):
            result = super().create_draft_pr(**kwargs)
            self.existing_pr = replace(self.existing_pr, head_sha="c" * 40)
            return result

    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(SpecPhaseError, match="delivery verification failed"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=StaleGitHub(),
            harness=FakeHarness(),
            workspace=workspace,
            claim=FakeClaim(),
        )

    assert ("cleanup", False) in workspace.calls


def test_post_delivery_rejects_cross_repository_pr_collision(tmp_path):
    class ForkGitHub(FakeGitHub):
        def create_draft_pr(self, **kwargs):
            result = super().create_draft_pr(**kwargs)
            self.existing_pr = replace(
                self.existing_pr,
                is_cross_repository=True,
                head_repository="attacker/demo",
            )
            return result

    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(SpecPhaseError, match="cross-repository"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=ForkGitHub(),
            harness=FakeHarness(),
            workspace=workspace,
            claim=FakeClaim(),
        )

    assert ("cleanup", False) in workspace.calls


def test_post_delivery_rejects_pr_retargeted_to_another_base(tmp_path):
    class RetargetedGitHub(FakeGitHub):
        def create_draft_pr(self, **kwargs):
            result = super().create_draft_pr(**kwargs)
            self.existing_pr = replace(self.existing_pr, base="release")
            return result

    workspace = FakeWorkspace(tmp_path)

    with pytest.raises(SpecPhaseError, match="base 'release' != 'main'"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=RetargetedGitHub(),
            harness=FakeHarness(),
            workspace=workspace,
            claim=FakeClaim(),
        )

    assert ("cleanup", False) in workspace.calls


def test_spec_instructions_come_from_provisioned_task_head_and_are_checkpointed(
    tmp_path,
):
    controller = tmp_path / "controller"
    controller.mkdir()
    (controller / "AGENTS.md").write_text("controller-only uncommitted rules")

    class InstructionWorkspace(FakeWorkspace):
        repo_root = controller

        def provision(self, *args, **kwargs):
            path = super().provision(*args, **kwargs)
            (path / "AGENTS.md").write_text("rules from the provisioned task head")
            return path

    config = MachinistConfig.model_validate(
        {"instructions": {"spec": {"paths": ["AGENTS.md"]}}}
    )
    harness = FakeHarness()
    claim = FakeClaim()

    run_spec_phase(
        42,
        config,
        github=FakeGitHub(),
        harness=harness,
        workspace=InstructionWorkspace(tmp_path),
        claim=claim,
    )

    prompt = harness.prompts[0][0]
    assert "rules from the provisioned task head" in prompt
    assert "controller-only uncommitted rules" not in prompt
    assert (
        claim.evidence["instructions_sha256"]
        == hashlib.sha256(b"rules from the provisioned task head").hexdigest()
    )
    assert claim.evidence["instruction_paths"] == ["AGENTS.md"]
    assert claim.evidence["instruction_append"] is False
    assert "instruction_source" not in claim.evidence


def test_spec_delivery_persists_no_observed_pr_copies(tmp_path):
    claim = FakeClaim()

    run_spec_phase(
        42,
        MachinistConfig(),
        github=FakeGitHub(),
        harness=FakeHarness(),
        workspace=FakeWorkspace(tmp_path),
        claim=claim,
    )

    assert claim.evidence["pr_number"] == 57
    for key in ("pr_observed_number", "pr_observed_base", "pr_observed_sha"):
        assert key not in claim.evidence


def test_spec_requires_a_claim(tmp_path):
    with pytest.raises(TypeError, match="claim"):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=FakeGitHub(),
            harness=FakeHarness(),
            workspace=FakeWorkspace(tmp_path),
        )


def test_run_spec_phase_returns_the_observed_pull_request(tmp_path):
    github = FakeGitHub()

    result = run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(),
        workspace=FakeWorkspace(tmp_path),
        claim=FakeClaim(),
    )

    assert result.number == 57
    assert result.head_sha == "b" * 40
    assert result.branch == "agent/issue-42"


def test_spec_rejects_an_existing_pr_on_another_branch_through_custody(tmp_path):
    stray = PullRequest(
        number=57,
        title="Spec",
        url="https://github.com/vscarpenter/demo/pull/57",
        branch="agent/issue-99",
        is_draft=True,
        head_sha="b" * 40,
        base="main",
    )
    github = FakeGitHub(existing_pr=stray)
    workspace = FakeWorkspace(tmp_path)
    workspace._remote_sha = stray.head_sha

    with pytest.raises(
        SpecPhaseError, match=r"branch 'agent/issue-99' != 'agent/issue-42'"
    ):
        run_spec_phase(
            42,
            MachinistConfig(),
            github=github,
            harness=FakeHarness(),
            workspace=workspace,
            revise=True,
            claim=FakeClaim(),
        )


def test_spec_uses_the_shared_approved_label_metadata(tmp_path):
    from machinist.github import APPROVED_LABEL_COLOR, APPROVED_LABEL_DESCRIPTION

    github = FakeGitHub()

    run_spec_phase(
        42,
        MachinistConfig(),
        github=github,
        harness=FakeHarness(),
        workspace=FakeWorkspace(tmp_path),
        claim=FakeClaim(),
    )

    assert github.labels["machinist:approved"] == (
        APPROVED_LABEL_COLOR,
        APPROVED_LABEL_DESCRIPTION,
    )

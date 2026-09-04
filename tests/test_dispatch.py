"""Contracts for Phase Task Run construction outside the CLI."""

import inspect
from types import SimpleNamespace

import pytest

from machinist.config import MachinistConfig
from machinist.dispatch import TaskDispatcher
from machinist.github import DraftPR, PullRequest
from machinist.lifecycle import LifecycleError, Phase, RunRecord, RunStatus


class FakeLifecycle:
    def __init__(self, execute_record: RunRecord | None = None):
        self.execute_record = execute_record
        self.calls = []

    def run(self, issue, phase, action, *, repeat_succeeded=False):
        claim = SimpleNamespace(attempt=2, previous_evidence={"prior": "evidence"})
        self.calls.append((issue, phase, repeat_succeeded))
        return action(claim)

    def record(self, issue, phase):
        assert issue == 42
        assert phase is Phase.EXECUTE
        return self.execute_record


class FakeCancellation:
    def check(self, issue):
        return lambda: issue == -1


def pull_request() -> PullRequest:
    return PullRequest(
        number=57,
        title="Task",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        is_draft=True,
    )


def successful_execute() -> RunRecord:
    return RunRecord(
        issue=42,
        phase=Phase.EXECUTE,
        status=RunStatus.SUCCEEDED,
        attempt=1,
        started_at="2026-09-02T00:00:00+00:00",
        updated_at="2026-09-02T00:01:00+00:00",
        evidence={"implementation_sha": "a" * 40},
    )


def dispatcher(tmp_path, lifecycle, calls) -> TaskDispatcher:
    def spec_runner(issue, config, **kwargs):
        calls.append(("spec", issue, kwargs))
        return DraftPR(number=57, url="https://github.com/x/y/pull/57")

    def execute_runner(issue, config, **kwargs):
        calls.append(("execute", issue, kwargs))
        return pull_request()

    def review_runner(issue, config, **kwargs):
        calls.append(("review", issue, kwargs))
        return pull_request()

    return TaskDispatcher(
        MachinistConfig(),
        repo_root=tmp_path,
        lifecycle=lifecycle,
        cancellation=FakeCancellation(),
        github=object(),
        harness_factory=lambda phase, issue: ("harness", phase, issue),
        workspace_factory=lambda: "workspace",
        spec_runner=spec_runner,
        execute_runner=execute_runner,
        review_runner=review_runner,
        test_runner=lambda *args, **kwargs: None,
    )


def test_spec_dispatch_constructs_dependencies_and_attempt_scope(tmp_path):
    lifecycle = FakeLifecycle()
    calls = []

    result = dispatcher(tmp_path, lifecycle, calls).run_spec(42, revise=True)

    assert result.number == 57
    assert lifecycle.calls == [(42, Phase.SPEC, True)]
    kind, issue, kwargs = calls[0]
    assert (kind, issue) == ("spec", 42)
    assert kwargs["harness"] == ("harness", Phase.SPEC, 42)
    assert kwargs["workspace"] == "workspace"
    assert kwargs["revise"] is True
    assert kwargs["attempt"] == 2
    assert callable(kwargs["cancel_check"])


def test_external_dependencies_are_constructed_only_inside_the_claim(tmp_path):
    lifecycle = FakeLifecycle()
    observed = []

    def github_factory():
        observed.append(tuple(lifecycle.calls))
        return object()

    dispatcher = TaskDispatcher(
        MachinistConfig(),
        repo_root=tmp_path,
        lifecycle=lifecycle,
        cancellation=FakeCancellation(),
        github_factory=github_factory,
        harness_factory=lambda phase, issue: object(),
        workspace_factory=lambda: object(),
        spec_runner=lambda *args, **kwargs: DraftPR(number=57, url="pr"),
    )

    dispatcher.run_spec(42)

    assert observed == [((42, Phase.SPEC, False),)]


@pytest.mark.parametrize(
    ("force", "feedback", "repeat_succeeded"),
    [(False, None, False), (True, "Keep the API stable.", True)],
)
def test_execute_dispatch_owns_normal_retry_and_amendment_options(
    tmp_path,
    force,
    feedback,
    repeat_succeeded,
):
    lifecycle = FakeLifecycle()
    calls = []

    dispatcher(tmp_path, lifecycle, calls).run_execute(
        42,
        force=force,
        resume=False,
        feedback=feedback,
    )

    assert lifecycle.calls == [(42, Phase.EXECUTE, repeat_succeeded)]
    kind, issue, kwargs = calls[0]
    assert (kind, issue) == ("execute", 42)
    assert kwargs["harness"] == ("harness", Phase.EXECUTE, 42)
    assert kwargs["workspace"] == "workspace"
    assert kwargs["force"] is force
    assert kwargs["resume"] is False
    assert kwargs.get("feedback") == feedback
    assert callable(kwargs["test_runner"])
    assert callable(kwargs["cancel_check"])


def test_retry_now_dispatches_the_selected_phase_with_recovery(tmp_path):
    lifecycle = FakeLifecycle()
    calls = []

    dispatcher(tmp_path, lifecycle, calls).run_phase(
        42,
        Phase.EXECUTE,
        resume=True,
    )

    assert lifecycle.calls == [(42, Phase.EXECUTE, False)]
    assert calls[0][2]["resume"] is True


def test_review_dispatch_requires_execute_success_and_passes_its_evidence(tmp_path):
    lifecycle = FakeLifecycle(successful_execute())
    calls = []

    dispatcher(tmp_path, lifecycle, calls).run_review(42)

    assert lifecycle.calls == [(42, Phase.REVIEW, False)]
    kind, issue, kwargs = calls[0]
    assert (kind, issue) == ("review", 42)
    assert kwargs["harness"] == ("harness", Phase.REVIEW, 42)
    assert kwargs["execute_evidence"] == {"implementation_sha": "a" * 40}
    assert callable(kwargs["cancel_check"])


def test_review_dispatch_rejects_missing_successful_execute(tmp_path):
    lifecycle = FakeLifecycle()

    with pytest.raises(LifecycleError, match="no successful Execute"):
        dispatcher(tmp_path, lifecycle, []).run_review(42)


def test_cli_contains_no_phase_lifecycle_construction():
    import machinist.cli as cli_module

    assert "lifecycle.run(" not in inspect.getsource(cli_module)


def test_run_spec_forwards_a_plain_revise_flag_by_default(tmp_path):
    lifecycle = FakeLifecycle()
    calls: list = []

    dispatcher(tmp_path, lifecycle, calls).run_spec(42)

    kwargs = calls[0][2]
    assert kwargs["revise"] is False


def test_preview_spec_builds_dependencies_without_a_task_run(tmp_path):
    lifecycle = FakeLifecycle()
    calls = []

    def preview_runner(issue, config, **kwargs):
        calls.append(("preview", issue, kwargs))
        return "## Preview\n"

    dispatcher = TaskDispatcher(
        MachinistConfig(),
        repo_root=tmp_path,
        lifecycle=lifecycle,
        cancellation=FakeCancellation(),
        github=object(),
        harness_factory=lambda phase, issue: ("harness", phase, issue),
        workspace_factory=lambda: "workspace",
        preview_runner=preview_runner,
    )

    assert dispatcher.preview_spec(42) == "## Preview\n"
    kind, issue, kwargs = calls[0]
    assert (kind, issue) == ("preview", 42)
    assert kwargs["harness"] == ("harness", Phase.SPEC, 42)
    assert kwargs["workspace"] == "workspace"
    assert callable(kwargs["cancel_check"])
    assert lifecycle.calls == []


def test_real_workshop_construction_wires_task_cancellation(tmp_path):
    from machinist.workspace import Workspace

    lifecycle = FakeLifecycle()
    calls = []

    def spec_runner(issue, config, **kwargs):
        calls.append(kwargs)
        return pull_request()

    TaskDispatcher(
        MachinistConfig(),
        repo_root=tmp_path,
        lifecycle=lifecycle,
        cancellation=FakeCancellation(),
        github=object(),
        harness_factory=lambda phase, issue: ("harness", phase, issue),
        spec_runner=spec_runner,
    ).run_spec(42)

    workspace = calls[0]["workspace"]
    assert isinstance(workspace, Workspace)
    assert callable(workspace.cancel_check)

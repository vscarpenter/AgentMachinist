"""Tests for the watch daemon's dispatch pass."""

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue, PullRequest
from machinist.phases.watch import WatchResult, WatchState, plan_watch_tasks, watch_once


def issue(number):
    return Issue(
        number=number,
        title=f"Issue {number}",
        body="",
        url=f"https://github.com/x/y/issues/{number}",
    )


def pr(number, branch, *, draft=True, labels=()):
    return PullRequest(
        number=number,
        title=f"PR {number}",
        url=f"https://github.com/x/y/pull/{number}",
        branch=branch,
        is_draft=draft,
        head_sha="a" * 40,
        labels=list(labels),
    )


class FakeGitHub:
    def __init__(self, issues=(), prs=(), approvals=None):
        self._issues = list(issues)
        self._prs = list(prs)
        self._approvals = approvals or {}

    def issues_with_label(self, label):
        return self._issues

    def open_machinist_prs(self, prefix):
        return self._prs

    def approval_sha(self, number):
        return self._approvals.get(number)


class Dispatcher:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, issue_number):
        self.calls.append(issue_number)
        if self.error:
            raise self.error
        return DraftPR(
            number=90 + issue_number,
            url=f"https://github.com/x/y/pull/{90 + issue_number}",
        )


def run(
    github,
    *,
    state=None,
    run_spec=None,
    run_execute=None,
    notify=None,
    max_tasks=None,
    admit=None,
):
    return watch_once(
        MachinistConfig(),
        github,
        run_spec=run_spec or Dispatcher(),
        run_execute=run_execute or Dispatcher(),
        state=state or WatchState(),
        notify=notify,
        max_tasks=max_tasks,
        admit=admit,
    )


def test_awaiting_spec_issue_dispatches_spec_phase():
    run_spec = Dispatcher()

    events = run(FakeGitHub(issues=[issue(7)]), run_spec=run_spec)

    assert run_spec.calls == [7]
    assert any("issue #7" in e for e in events)


def test_github_actions_spec_source_prevents_local_spec_dispatch():
    config = MachinistConfig.model_validate(
        {"github": {"spec_source": "github-actions"}}
    )
    run_spec = Dispatcher()

    events = watch_once(
        config,
        FakeGitHub(issues=[issue(7)]),
        run_spec=run_spec,
        run_execute=Dispatcher(),
        state=WatchState(),
    )

    assert run_spec.calls == []
    assert events == []


def test_approved_draft_pr_dispatches_execute_with_issue_number():
    run_execute = Dispatcher()
    github = FakeGitHub(
        prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])],
        approvals={57: "a" * 40},
    )

    events = run(github, run_execute=run_execute)

    assert run_execute.calls == [42]
    assert any("#42" in e for e in events)


def test_awaiting_approval_and_in_review_prs_dispatch_nothing():
    run_spec, run_execute = Dispatcher(), Dispatcher()
    github = FakeGitHub(
        prs=[
            pr(57, "agent/issue-42"),  # awaiting approval
            pr(
                58, "agent/issue-43", draft=False, labels=["machinist:approved"]
            ),  # in review
        ]
    )

    events = run(github, run_spec=run_spec, run_execute=run_execute)

    assert run_spec.calls == []
    assert run_execute.calls == []
    assert events == []


def test_dispatch_failure_becomes_structured_result_and_daemon_survives():
    run_spec = Dispatcher(error=RuntimeError("boom"))
    github = FakeGitHub(issues=[issue(7), issue(8)])
    state = WatchState()

    result = watch_once(
        MachinistConfig(),
        github,
        run_spec=run_spec,
        run_execute=Dispatcher(),
        state=state,
    )

    # Both issues were attempted despite the first failing.
    assert run_spec.calls == [7, 8]
    assert isinstance(result, WatchResult)
    assert sum("failed" in event for event in result.events) == 2
    assert [(failure.issue_number, failure.phase) for failure in result.failures] == [
        (7, "spec"),
        (8, "spec"),
    ]


def test_dispatch_failure_notifies_with_issue_and_reason():
    notifications = []
    run_spec = Dispatcher(error=RuntimeError("boom"))

    events = run(
        FakeGitHub(issues=[issue(7)]), run_spec=run_spec, notify=notifications.append
    )

    assert notifications == ["spec for issue #7 failed: boom"]
    assert events == ["error: spec for issue #7 failed: boom"]


def test_successful_dispatch_does_not_notify():
    notifications = []

    events = run(FakeGitHub(issues=[issue(7)]), notify=notifications.append)

    assert notifications == []
    assert any("draft PR" in e for e in events)


def test_failed_issue_is_reconsidered_on_next_pass_for_durable_retry():
    state = WatchState()
    failing = Dispatcher(error=RuntimeError("boom"))
    succeeding = Dispatcher()
    github = FakeGitHub(issues=[issue(7)])

    first = run(github, state=state, run_spec=failing)
    second = run(github, state=state, run_spec=succeeding)

    assert failing.calls == [7]
    assert succeeding.calls == [7]
    assert len(first.failures) == 1
    assert second.failures == ()
    assert any("draft PR" in event for event in second.events)


def test_repeated_identical_failure_is_retried_but_notification_is_deduplicated():
    state = WatchState()
    dispatcher = Dispatcher(error=RuntimeError("boom"))
    notifications = []
    github = FakeGitHub(issues=[issue(7)])

    run(github, state=state, run_spec=dispatcher, notify=notifications.append)
    run(github, state=state, run_spec=dispatcher, notify=notifications.append)

    assert dispatcher.calls == [7, 7]
    assert notifications == ["spec for issue #7 failed: boom"]


def test_stale_approval_notification_is_deduplicated_until_state_changes():
    state = WatchState()
    notifications = []
    stale = FakeGitHub(
        prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])],
        approvals={57: "b" * 40},
    )

    for _ in range(2):
        watch_once(
            MachinistConfig(),
            stale,
            run_spec=Dispatcher(),
            run_execute=Dispatcher(),
            state=state,
            notify_stale=lambda issue_number, message: notifications.append(
                (issue_number, message)
            ),
        )

    assert notifications == [(42, "PR #57 approval is stale")]


def test_execute_tasks_run_before_new_spec_tasks():
    order = []

    def run_spec(issue_number):
        order.append(("spec", issue_number))
        return DraftPR(number=97, url="https://github.com/x/y/pull/97")

    def run_execute(issue_number):
        order.append(("execute", issue_number))
        return DraftPR(number=57, url="https://github.com/x/y/pull/57")

    github = FakeGitHub(
        issues=[issue(7)],
        prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])],
        approvals={57: "a" * 40},
    )

    run(github, run_spec=run_spec, run_execute=run_execute)

    assert order == [("execute", 42), ("spec", 7)]


def test_max_tasks_defers_remaining_work_without_dispatching_it():
    run_spec = Dispatcher()

    result = run(
        FakeGitHub(issues=[issue(7), issue(8), issue(9)]),
        run_spec=run_spec,
        max_tasks=1,
    )

    assert run_spec.calls == [7]
    assert [(task.phase, task.issue_number) for task in result.attempted] == [
        ("spec", 7)
    ]
    assert [(task.phase, task.issue_number) for task in result.deferred] == [
        ("spec", 8),
        ("spec", 9),
    ]


def test_admission_hook_can_defer_selected_tasks():
    run_spec = Dispatcher()

    result = run(
        FakeGitHub(issues=[issue(7), issue(8)]),
        run_spec=run_spec,
        admit=lambda task: task.issue_number != 7,
    )

    assert run_spec.calls == [8]
    assert [(task.phase, task.issue_number) for task in result.deferred] == [
        ("spec", 7)
    ]


def test_plan_watch_tasks_is_a_dispatch_free_dry_run_surface():
    github = FakeGitHub(
        issues=[issue(7)],
        prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])],
        approvals={57: "a" * 40},
    )

    tasks = plan_watch_tasks(MachinistConfig(), github)

    assert [(task.phase, task.issue_number) for task in tasks] == [
        ("execute", 42),
        ("spec", 7),
    ]


def test_watch_result_remains_iterable_and_list_comparable_for_cli_compatibility():
    result = run(FakeGitHub(issues=[issue(7)]))

    assert list(result) == list(result.events)
    assert result == list(result.events)
    assert bool(result)


def test_max_tasks_must_not_be_negative():
    try:
        run(FakeGitHub(), max_tasks=-1)
    except ValueError as exc:
        assert "max_tasks" in str(exc)
    else:
        raise AssertionError("negative max_tasks should fail")

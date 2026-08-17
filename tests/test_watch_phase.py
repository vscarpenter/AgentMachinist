"""Tests for the watch daemon's dispatch pass."""

from machinist.config import MachinistConfig
from machinist.github import DraftPR, Issue, PullRequest
from machinist.phases.watch import WatchState, watch_once


def issue(number):
    return Issue(number=number, title=f"Issue {number}", body="",
                 url=f"https://github.com/x/y/issues/{number}")


def pr(number, branch, *, draft=True, labels=()):
    return PullRequest(number=number, title=f"PR {number}",
                       url=f"https://github.com/x/y/pull/{number}",
                       branch=branch, is_draft=draft, head_sha="a" * 40,
                       labels=list(labels))


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
        return DraftPR(number=90 + issue_number, url=f"https://github.com/x/y/pull/{90 + issue_number}")


def run(github, *, state=None, run_spec=None, run_execute=None, notify=None):
    return watch_once(
        MachinistConfig(),
        github,
        run_spec=run_spec or Dispatcher(),
        run_execute=run_execute or Dispatcher(),
        state=state or WatchState(),
        notify=notify,
    )


def test_awaiting_spec_issue_dispatches_spec_phase():
    run_spec = Dispatcher()

    events = run(FakeGitHub(issues=[issue(7)]), run_spec=run_spec)

    assert run_spec.calls == [7]
    assert any("issue #7" in e for e in events)


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
    github = FakeGitHub(prs=[
        pr(57, "agent/issue-42"),                                # awaiting approval
        pr(58, "agent/issue-43", draft=False, labels=["machinist:approved"]),  # in review
    ])

    events = run(github, run_spec=run_spec, run_execute=run_execute)

    assert run_spec.calls == []
    assert run_execute.calls == []
    assert events == []


def test_dispatch_failure_becomes_event_and_daemon_survives():
    run_spec = Dispatcher(error=RuntimeError("boom"))
    github = FakeGitHub(issues=[issue(7), issue(8)])
    state = WatchState()

    events = watch_once(MachinistConfig(), github, run_spec=run_spec,
                        run_execute=Dispatcher(), state=state)

    # Both issues were attempted despite the first failing.
    assert run_spec.calls == [7, 8]
    assert sum("failed" in e for e in events) == 2
    assert state.failed_issues == {7, 8}


def test_dispatch_failure_notifies_with_issue_and_reason():
    notifications = []
    run_spec = Dispatcher(error=RuntimeError("boom"))

    events = run(FakeGitHub(issues=[issue(7)]), run_spec=run_spec,
                 notify=notifications.append)

    assert notifications == ["spec for issue #7 failed: boom"]
    assert events == ["error: spec for issue #7 failed: boom"]


def test_successful_dispatch_does_not_notify():
    notifications = []

    events = run(FakeGitHub(issues=[issue(7)]), notify=notifications.append)

    assert notifications == []
    assert any("draft PR" in e for e in events)


def test_failed_issue_is_never_redispatched():
    run_spec = Dispatcher()
    state = WatchState(failed_issues={7})

    events = run(FakeGitHub(issues=[issue(7)]), state=state, run_spec=run_spec)

    assert run_spec.calls == []
    assert events == []

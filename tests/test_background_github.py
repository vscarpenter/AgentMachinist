"""Trusted delegation intake and exact-candidate remote CI contracts."""

import json
import subprocess

import pytest

from machinist.background_github import BackgroundGitHubClient
from machinist.github import GitHubError

SHA = "a" * 40
LABEL = "machinist:queue"


class Runner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return subprocess.CompletedProcess(args, 0, json.dumps(result), "")


def client(*responses, host="github.com"):
    runner = Runner(*responses)
    github = BackgroundGitHubClient(runner=runner)
    github.bind_repository("owner/repo", hostname=host)
    return github, runner


def issue(**changes):
    return {
        "number": 12,
        "title": "Fix escaping",
        "body": "Preserve commas.",
        "html_url": "https://github.com/owner/repo/issues/12",
        "state": "open",
        "labels": [{"name": LABEL}],
        "user": {"login": "untrusted-author"},
        **changes,
    }


def event(event_id=100, actor="delegator", **changes):
    return {
        "id": event_id,
        "event": "labeled",
        "label": {"name": LABEL},
        "actor": {"login": actor},
        "created_at": "2026-09-18T12:00:00Z",
        **changes,
    }


def queue_responses(*, permission="write", snapshot=None, events=None):
    snapshot = issue() if snapshot is None else snapshot
    events = [event()] if events is None else events
    return [
        [[snapshot]],
        [events],
        {"permission": permission, "user": {"login": events[-1]["actor"]["login"]}},
        snapshot,
        [events],
    ]


def check(name="tests", **changes):
    return {
        "id": 11,
        "name": name,
        "head_sha": SHA,
        "status": "completed",
        "conclusion": "success",
        **changes,
    }


def ci_responses(checks=None, statuses=None):
    return [[{"check_runs": checks or []}], [{"sha": SHA, "statuses": statuses or []}]]


def pr(**changes):
    return {
        "number": 23,
        "title": "Fix escaping",
        "url": "https://github.com/owner/repo/pull/23",
        "headRefName": "machinist/task-T1",
        "headRefOid": SHA,
        "isDraft": True,
        "state": "OPEN",
        "labels": [],
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": "owner/repo"},
        "baseRefName": "main",
        **changes,
    }


def test_queue_uses_actual_label_actor_and_stable_event_identity():
    github, runner = client(*queue_responses())
    (queued,) = github.queued_tasks(LABEL)
    assert (
        queued.number,
        queued.title,
        queued.body,
        queued.actor,
        queued.event_id,
    ) == (12, "Fix escaping", "Preserve commas.", "delegator", "100")
    assert queued.queued_at == "2026-09-18T12:00:00Z"
    assert any(
        "repos/owner/repo/collaborators/delegator/permission" in call
        for call in runner.calls
    )
    assert all("untrusted-author" not in arg for call in runner.calls for arg in call)
    assert "--paginate" in runner.calls[0] and "--slurp" in runner.calls[0]
    assert "--paginate" in runner.calls[1]


@pytest.mark.parametrize("permission", ["read", "triage", "none", "maintain", ""])
def test_permission_below_actual_write_never_delegates(permission):
    github, _ = client(*queue_responses(permission=permission)[:3])
    assert github.queued_tasks(LABEL) == ()


def test_permission_transport_failure_is_visible_and_never_admits():
    responses = queue_responses()
    responses[2] = subprocess.TimeoutExpired("gh", 30)
    github, _ = client(*responses)
    with pytest.raises(GitHubError, match="timed out"):
        github.queued_tasks(LABEL)


def test_latest_actual_queue_label_event_controls_actor():
    events = [event(99, "old-delegator"), event(100, "delegator")]
    github, runner = client(*queue_responses(events=events))
    assert github.queued_tasks(LABEL)[0].actor == "delegator"
    assert "repos/owner/repo/collaborators/delegator/permission" in runner.calls[2]


def test_removed_label_has_no_authorization_even_if_list_is_stale():
    github, _ = client([[issue()]], [[event(), event(101, event="unlabeled")]])
    assert github.queued_tasks(LABEL) == ()


def test_queue_is_paginated_and_deduplicated_before_permission_lookup():
    responses = queue_responses()
    responses[0] = [[issue()], [issue()]]
    responses[1] = [[event()], [event()]]
    github, runner = client(*responses)
    assert len(github.queued_tasks(LABEL)) == 1
    assert len(runner.calls) == 5


@pytest.mark.parametrize("change", [{"body": "Changed"}, {"title": "Changed"}])
def test_issue_edit_during_intake_requires_a_new_snapshot(change):
    responses = queue_responses()
    responses[3] = issue(**change)
    github, _ = client(*responses)
    with pytest.raises(GitHubError, match="changed during queue intake"):
        github.queued_tasks(LABEL)


def test_relabel_during_intake_is_not_misattributed_to_old_actor():
    responses = queue_responses()
    responses[4] = [[event(), event(101, "other")]]
    github, _ = client(*responses)
    with pytest.raises(GitHubError, match="changed during queue intake"):
        github.queued_tasks(LABEL)


@pytest.mark.parametrize("change", [{"state": "closed"}, {"labels": []}])
def test_removed_queue_or_closed_issue_is_not_admitted(change):
    responses = queue_responses()
    responses[3] = issue(**change)
    github, _ = client(*responses)
    assert github.queued_tasks(LABEL) == ()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/owner/repo/issues/12",
        "https://github.com/other/repo/issues/12",
        "https://github.com/owner/repo/issues/99",
    ],
)
def test_foreign_issue_identity_is_rejected(url):
    github, _ = client([[issue(html_url=url)]])
    with pytest.raises(GitHubError, match="identity"):
        github.queued_tasks(LABEL)


def test_pull_requests_returned_by_issue_endpoint_are_not_tasks():
    github, runner = client([[issue(pull_request={"url": "unused"})]])
    assert github.queued_tasks(LABEL) == ()
    assert len(runner.calls) == 1


def test_unbound_repository_is_rejected_before_transport():
    github = BackgroundGitHubClient(repo="owner/repo", runner=Runner())
    with pytest.raises(GitHubError, match="bound"):
        github.queued_tasks(LABEL)


def test_enterprise_queue_binds_host_and_checks_issue_identity():
    snapshot = issue(html_url="https://ghe.example.test/owner/repo/issues/12")
    github, runner = client(
        *queue_responses(snapshot=snapshot), host="ghe.example.test"
    )
    assert github.queued_tasks(LABEL)[0].number == 12
    assert all(call[-2:] == ["--hostname", "ghe.example.test"] for call in runner.calls)


def test_ci_requires_explicit_names_without_making_requests():
    github, runner = client()
    assert github.observe_ci(SHA, ()).status == "unknown"
    assert runner.calls == []


def test_ci_missing_required_check_never_passes():
    github, _ = client(*ci_responses([check("lint")]))
    observation = github.observe_ci(SHA, ("tests", "lint"))
    assert observation.status == "unknown"
    assert "tests" in observation.summary


def test_ci_all_required_contexts_must_pass_on_exact_sha_across_pages():
    github, runner = client(
        [{"check_runs": [check("tests")]}, {"check_runs": [check("lint")]}],
        [
            {
                "sha": SHA,
                "statuses": [{"id": 22, "context": "deploy", "state": "success"}],
            }
        ],
    )
    assert github.observe_ci(SHA, ("tests", "lint", "deploy")).status == "passed"
    assert all(SHA in " ".join(call) and "--paginate" in call for call in runner.calls)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"status": "in_progress", "conclusion": None}, "pending"),
        ({"conclusion": "failure"}, "failed"),
        ({"conclusion": "cancelled"}, "failed"),
        ({"conclusion": "skipped"}, "unknown"),
        ({"status": "unrecognized"}, "unknown"),
        ({"head_sha": "b" * 40}, "unknown"),
    ],
)
def test_ci_does_not_treat_incomplete_or_wrong_candidate_as_success(changes, expected):
    github, _ = client(*ci_responses([check(**changes)]))
    assert github.observe_ci(SHA, ("tests",)).status == expected


def test_status_latest_failure_overrides_old_success():
    github, _ = client(
        *ci_responses(
            statuses=[
                {"id": 23, "context": "tests", "state": "failure"},
                {"id": 22, "context": "tests", "state": "success"},
            ]
        )
    )
    assert github.observe_ci(SHA, ("tests",)).status == "failed"


def test_conflicting_same_named_check_sources_cannot_hide_failure():
    github, _ = client(*ci_responses([check(), check(id=12, conclusion="failure")]))
    assert github.observe_ci(SHA, ("tests",)).status == "failed"


def test_status_response_for_wrong_commit_is_unknown():
    responses = ci_responses([check()])
    responses[1][0]["sha"] = "b" * 40
    github, _ = client(*responses)
    assert github.observe_ci(SHA, ("tests",)).status == "unknown"


def test_ci_transport_error_is_visible():
    github, _ = client(subprocess.TimeoutExpired("gh", 30))
    with pytest.raises(GitHubError, match="timed out"):
        github.observe_ci(SHA, ("tests",))


@pytest.mark.parametrize("sha", ["main", "a" * 39, "../bad", "A" * 40])
def test_ci_rejects_noncanonical_exact_sha_before_transport(sha):
    github, runner = client()
    with pytest.raises(GitHubError, match="SHA"):
        github.observe_ci(sha, ("tests",))
    assert runner.calls == []


def test_ready_transition_checks_exact_candidate_before_and_after():
    github, runner = client(pr(), {}, pr(isDraft=False))
    github.request_ready(
        23, SHA, expected_branch="machinist/task-T1", expected_base="main"
    )
    assert runner.calls[1][:4] == ["gh", "pr", "ready", "23"]
    assert len(runner.calls) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"headRefOid": "b" * 40},
        {"isCrossRepository": True},
        {"headRepository": {"nameWithOwner": "evil/repo"}},
        {"state": "CLOSED"},
        {"number": 24},
        {"headRefName": "other"},
        {"baseRefName": "other"},
    ],
)
def test_ready_rejects_wrong_pr_or_candidate_without_mutation(change):
    github, runner = client(pr(**change))
    with pytest.raises(GitHubError, match="custody"):
        github.request_ready(
            23, SHA, expected_branch="machinist/task-T1", expected_base="main"
        )
    assert len(runner.calls) == 1


def test_ready_cancel_between_read_and_mutation_has_no_side_effect():
    github, runner = client(pr())

    def cancelled():
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        github.request_ready(23, SHA, cancel_check=cancelled)
    assert not any(call[1:3] == ["pr", "ready"] for call in runner.calls)


def test_ready_observed_race_restores_draft_and_reports_failure():
    github, runner = client(pr(), {}, pr(isDraft=False, headRefOid="b" * 40), {})
    with pytest.raises(GitHubError, match="custody"):
        github.request_ready(23, SHA)
    assert "--undo" in runner.calls[-1]


def test_ready_already_ready_exact_candidate_is_idempotent():
    github, runner = client(pr(isDraft=False))
    github.request_ready(23, SHA)
    assert len(runner.calls) == 1


def test_open_bot_pr_count_reads_all_pages_and_excludes_forks():
    def remote(number, *, prefix="machinist/task-", repository="owner/repo"):
        return {
            "number": number,
            "state": "open",
            "html_url": f"https://github.com/owner/repo/pull/{number}",
            "head": {"ref": f"{prefix}{number}", "repo": {"full_name": repository}},
            "base": {"repo": {"full_name": "owner/repo"}},
        }

    github, runner = client(
        [
            [remote(1, prefix="feature/"), remote(2, repository="fork/repo")],
            [remote(3), remote(4), remote(4)],
        ]
    )
    assert github.open_bot_pr_count("machinist/task-") == 2
    assert "--paginate" in runner.calls[0]


def test_ready_malformed_post_transition_response_restores_draft():
    github, runner = client(pr(), {}, [], {})
    with pytest.raises(GitHubError, match="invalid PR metadata"):
        github.request_ready(23, SHA)
    assert "--undo" in runner.calls[-1]


@pytest.mark.parametrize(
    "change", [{"isDraft": "false"}, {"url": "https://evil.test/owner/repo/pull/23"}]
)
def test_ready_invalid_custody_types_and_urls_fail_before_mutation(change):
    github, runner = client(pr(**change))
    with pytest.raises(GitHubError, match="custody"):
        github.request_ready(23, SHA)
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "100"),
        ("id", True),
        ("created_at", "invalid"),
        ("created_at", "2026-09-18"),
        ("actor", {"login": "../evil"}),
    ],
)
def test_malformed_queue_event_cannot_delegate(field, value):
    github, _ = client([[issue()]], [[event(**{field: value})]])
    with pytest.raises(GitHubError, match="identity"):
        github.queued_tasks(LABEL)


def test_conflicting_duplicate_queue_event_is_rejected():
    github, _ = client([[issue()]], [[event(), event(actor="other")]])
    with pytest.raises(GitHubError, match="conflicting queue event"):
        github.queued_tasks(LABEL)


def test_permission_response_cannot_authorize_a_different_actor():
    responses = queue_responses()
    responses[2]["user"]["login"] = "other"
    github, _ = client(*responses)
    with pytest.raises(GitHubError, match="identity mismatch"):
        github.queued_tasks(LABEL)


@pytest.mark.parametrize(
    "change",
    [{"body": []}, {"number": True}, {"html_url": "https://[bad/owner/repo/issues/12"}],
)
def test_malformed_issue_snapshot_fails_closed(change):
    github, _ = client([[issue(**change)]])
    with pytest.raises(GitHubError):
        github.queued_tasks(LABEL)


@pytest.mark.parametrize(
    "state,expected",
    [("pending", "pending"), ("error", "failed"), ("unknown", "unknown")],
)
def test_commit_status_context_observations(state, expected):
    github, _ = client(
        *ci_responses(statuses=[{"id": 22, "context": "tests", "state": state}])
    )
    assert github.observe_ci(SHA, ("tests",)).status == expected


def test_ready_boolean_cancellation_callback_prevents_transition():
    github, runner = client(pr())
    with pytest.raises(GitHubError, match="cancelled"):
        github.request_ready(23, SHA, cancel_check=lambda: True)
    assert not any(call[1:3] == ["pr", "ready"] for call in runner.calls)

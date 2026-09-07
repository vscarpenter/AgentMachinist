"""Publication preserves the delivered candidate across forge boundaries."""

import json
import subprocess
from dataclasses import replace

import pytest

from machinist.forge import (
    ForgeError,
    GitHubForgeClient,
    PublishedChange,
    publish_change,
)

SHA = "a" * 40
CHANGE = PublishedChange(
    "gitlab",
    "gitlab.com",
    "team/demo",
    7,
    "https://gitlab.com/team/demo/-/merge_requests/7",
    "machinist/task-1",
    "main",
    SHA,
    "OPEN",
    True,
)


class Forge:
    provider = "gitlab"
    host = "gitlab.com"
    repository = "team/demo"

    def __init__(self, existing=None, delivered=CHANGE):
        self.existing = existing
        self.delivered = delivered
        self.calls = []

    def find_change(self, branch):
        self.calls.append(("find", branch))
        return self.existing

    def create_change(self, **kwargs):
        self.calls.append(("create", kwargs))
        return self.delivered

    def update_change(self, number, **kwargs):
        self.calls.append(("update", number, kwargs))
        self.delivered = replace(self.delivered, is_draft=kwargs["draft"])
        return self.delivered

    def get_change(self, number):
        self.calls.append(("get", number))
        return self.delivered


def publish(forge, **kwargs):
    return publish_change(
        forge,
        branch=CHANGE.branch,
        base="main",
        head_sha=SHA,
        title="Implement an objective",
        body="Review evidence",
        **kwargs,
    )


def test_first_publication_creates_draft_and_proves_head_before_ready():
    forge = Forge()
    result = publish(forge)
    assert not result.is_draft
    assert [call[0] for call in forge.calls] == ["find", "create", "update", "get"]
    assert forge.calls[1][1]["draft"] is True


def test_retry_recovers_existing_candidate_without_duplicate_creation():
    forge = Forge(existing=CHANGE)
    assert publish(forge, draft=True).is_draft
    assert [call[0] for call in forge.calls] == ["find", "update", "get"]


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "github"},
        {"host": "attacker.example"},
        {"repository": "other/demo"},
        {"head_sha": "b" * 40},
        {"branch": "wrong"},
        {"base": "release"},
        {"state": "MERGED"},
    ],
)
def test_publication_rejects_existing_mismatched_candidate_before_mutation(changes):
    forge = Forge(existing=replace(CHANGE, **changes))
    with pytest.raises(ForgeError, match="custody"):
        publish(forge)
    assert [call[0] for call in forge.calls] == ["find"]


def test_changed_creation_head_is_never_promoted():
    forge = Forge(delivered=replace(CHANGE, head_sha="b" * 40))
    with pytest.raises(ForgeError, match="head"):
        publish(forge)
    assert [call[0] for call in forge.calls] == ["find", "create"]


def test_publication_rechecks_head_after_update():
    class MovingForge(Forge):
        def get_change(self, number):
            return replace(self.delivered, head_sha="b" * 40)

    with pytest.raises(ForgeError, match="head"):
        publish(MovingForge(existing=CHANGE))


@pytest.mark.parametrize("existing", [None, replace(CHANGE, number=8)])
def test_checkpointed_change_number_cannot_be_replaced(existing):
    forge = Forge(existing=existing)
    with pytest.raises(ForgeError, match="number"):
        publish(forge, expected_number=7)
    assert [call[0] for call in forge.calls] == ["find"]


def test_github_adapter_reuses_bound_client_and_checks_exact_pull_request():
    calls = []
    payload = {
        "number": 7,
        "title": "Ready",
        "url": "https://github.com/team/demo/pull/7",
        "headRefName": CHANGE.branch,
        "headRefOid": SHA,
        "isDraft": False,
        "state": "OPEN",
        "labels": [],
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": "team/demo"},
        "baseRefName": "main",
    }

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    forge = GitHubForgeClient("team/demo", runner=runner)
    change = forge.get_change(7)
    assert change.provider == "github"
    assert change.head_sha == SHA
    assert calls[0][:4] == ["gh", "pr", "view", "7"]
    assert calls[0][-2:] == ["--repo", "team/demo"]
    payload["isCrossRepository"] = True
    with pytest.raises(ForgeError, match="repository"):
        forge.get_change(7)


def github_payload(draft=True):
    return {
        "number": 7,
        "title": "Ready",
        "url": "https://github.com/team/demo/pull/7",
        "headRefName": CHANGE.branch,
        "headRefOid": SHA,
        "isDraft": draft,
        "state": "OPEN",
        "labels": [],
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": "team/demo"},
        "baseRefName": "main",
    }


@pytest.mark.parametrize("draft", [True, False])
def test_github_update_does_not_repeat_an_already_satisfied_draft_transition(draft):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["pr", "ready"]:
            return subprocess.CompletedProcess(argv, 1, "", "already in that state")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(github_payload(draft)), ""
        )

    forge = GitHubForgeClient("team/demo", runner=runner)
    assert (
        forge.update_change(7, title="Refined", body="Report", draft=draft).is_draft
        is draft
    )
    assert not any(argv[1:3] == ["pr", "ready"] for argv in calls)


def test_github_publication_creates_verified_draft_then_delivers_ready_change():
    responses = iter(
        [
            [],
            "https://github.com/team/demo/pull/7",
            github_payload(),
            "",
            github_payload(),
            "",
            github_payload(False),
            github_payload(False),
        ]
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        response = next(responses)
        output = response if isinstance(response, str) else json.dumps(response)
        return subprocess.CompletedProcess(argv, 0, output, "")

    result = publish(GitHubForgeClient("team/demo", runner=runner))
    assert result.provider == "github"
    assert not result.is_draft
    assert [argv[1:3] for argv in calls].count(["pr", "create"]) == 1
    assert [argv[1:3] for argv in calls].count(["pr", "ready"]) == 1


def test_github_task_intake_and_default_branch_keep_host_binding():
    responses = iter(
        [
            {
                "number": 2,
                "title": "Objective",
                "body": "Acceptance",
                "url": "https://github.example/team/demo/issues/2",
            },
            {"defaultBranchRef": {"name": "trunk"}},
        ]
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(responses)), "")

    forge = GitHubForgeClient("team/demo", host="github.example", runner=runner)
    assert forge.get_issue(2).body == "Acceptance"
    assert forge.default_branch() == "trunk"
    assert all("github.example/team/demo" in argv for argv in calls)

"""Tests for the gh-CLI-backed GitHub wrapper."""

import json
import subprocess

import pytest

from machinist.github import DraftPR, GitHubClient, GitHubError, Issue, PullRequest


class FakeRunner:
    """Records gh invocations and replays canned results."""

    def __init__(self, *results):
        self.calls = []
        self.kwargs = []
        self._results = list(results)

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(dict(kwargs))
        if not self._results:
            raise AssertionError(f"unexpected gh call: {args}")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        stdout, returncode, stderr = result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


ISSUE_JSON = json.dumps(
    {
        "number": 42,
        "title": "Add dark mode",
        "body": "Users want dark mode.",
        "url": "https://github.com/vscarpenter/demo/issues/42",
        "labels": [{"name": "agent-task"}, {"name": "enhancement"}],
    }
)
_MISSING = object()


def test_get_issue_builds_argv_and_parses_json():
    runner = FakeRunner((ISSUE_JSON, 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    issue = client.get_issue(42)

    assert runner.calls == [
        [
            "gh",
            "issue",
            "view",
            "42",
            "--json",
            "number,title,body,url,labels",
            "--repo",
            "vscarpenter/demo",
        ]
    ]
    assert issue == Issue(
        number=42,
        title="Add dark mode",
        body="Users want dark mode.",
        url="https://github.com/vscarpenter/demo/issues/42",
        labels=["agent-task", "enhancement"],
    )


def test_repo_flag_omitted_when_unset():
    runner = FakeRunner((ISSUE_JSON, 0, ""))
    client = GitHubClient(runner=runner)

    client.get_issue(42)

    assert "--repo" not in runner.calls[0]


def test_bound_github_dotcom_target_ignores_ambient_repo_and_host(monkeypatch):
    monkeypatch.setenv("GH_REPO", "attacker/other")
    monkeypatch.setenv("GH_HOST", "ghe.attacker.test")
    monkeypatch.setenv("GH_TOKEN", "dotcom-token")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "enterprise-token")
    runner = FakeRunner((ISSUE_JSON, 0, ""))
    client = GitHubClient(runner=runner)
    client.bind_repository("VSCarpenter/Demo", hostname="GitHub.com")

    client.get_issue(42)

    assert runner.calls[0][-2:] == ["--repo", "vscarpenter/demo"]
    environment = runner.kwargs[0]["env"]
    assert "GH_REPO" not in environment
    assert "GH_HOST" not in environment
    assert environment["GH_TOKEN"] == "dotcom-token"
    assert "GH_ENTERPRISE_TOKEN" not in environment


def test_bound_enterprise_target_does_not_receive_dotcom_token(monkeypatch):
    monkeypatch.setenv("GH_REPO", "attacker/other")
    monkeypatch.setenv("GH_HOST", "ghe.example.test:8443")
    monkeypatch.setenv("GH_TOKEN", "dotcom-token")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "enterprise-token")
    runner = FakeRunner((ISSUE_JSON, 0, ""))
    client = GitHubClient(runner=runner)
    client.bind_repository("VSCarpenter/Demo", hostname="ghe.example.test:8443")

    client.get_issue(42)

    assert runner.calls[0][-2:] == [
        "--repo",
        "ghe.example.test:8443/vscarpenter/demo",
    ]
    environment = runner.kwargs[0]["env"]
    assert "GH_REPO" not in environment
    assert "GH_HOST" not in environment
    assert "GH_TOKEN" not in environment
    assert environment["GH_ENTERPRISE_TOKEN"] == "enterprise-token"


def test_create_draft_pr_returns_number_and_url():
    url = "https://github.com/vscarpenter/demo/pull/57"
    runner = FakeRunner((url + "\n", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    pr = client.create_draft_pr(
        branch="agent/issue-42",
        base="main",
        title="Spec: Add dark mode",
        body="Closes #42",
    )

    assert runner.calls == [
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--head",
            "agent/issue-42",
            "--base",
            "main",
            "--title",
            "Spec: Add dark mode",
            "--body",
            "Closes #42",
            "--repo",
            "vscarpenter/demo",
        ]
    ]
    assert pr == DraftPR(number=57, url=url)


def test_create_issue_and_apply_dispatch_label_use_bounded_argv():
    url = "https://github.com/vscarpenter/demo/issues/42"
    runner = FakeRunner((url + "\n", 0, ""), ("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    issue = client.create_issue(title="Improve recovery", body="## Objective\nClear")
    client.add_issue_label(issue.number, "agent-task")

    assert issue == Issue(
        number=42,
        title="Improve recovery",
        body="## Objective\nClear",
        url=url,
    )
    assert runner.calls[0][:7] == [
        "gh",
        "issue",
        "create",
        "--title",
        "Improve recovery",
        "--body",
        "## Objective\nClear",
    ]
    assert runner.calls[1][:6] == [
        "gh",
        "issue",
        "edit",
        "42",
        "--add-label",
        "agent-task",
    ]


def test_ensure_label_is_idempotent_via_force():
    runner = FakeRunner(("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    client.ensure_label(
        "machinist:approved", color="0e8a16", description="Spec approved"
    )

    assert runner.calls == [
        [
            "gh",
            "label",
            "create",
            "machinist:approved",
            "--force",
            "--color",
            "0e8a16",
            "--description",
            "Spec approved",
            "--repo",
            "vscarpenter/demo",
        ]
    ]


def test_issues_with_label_builds_argv_and_parses():
    runner = FakeRunner((json.dumps([json.loads(ISSUE_JSON)]), 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    issues = client.issues_with_label("agent-task")

    assert runner.calls == [
        [
            "gh",
            "issue",
            "list",
            "--label",
            "agent-task",
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,body,url,labels",
            "--repo",
            "vscarpenter/demo",
        ]
    ]
    assert issues == [
        Issue(
            number=42,
            title="Add dark mode",
            body="Users want dark mode.",
            url="https://github.com/vscarpenter/demo/issues/42",
            labels=["agent-task", "enhancement"],
        )
    ]


def test_open_machinist_prs_filters_by_branch_prefix():
    payload = json.dumps(
        [
            {
                "number": 57,
                "title": "Spec: Add dark mode (#42)",
                "url": "https://github.com/vscarpenter/demo/pull/57",
                "headRefName": "agent/issue-42",
                "headRefOid": "a" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [{"name": "machinist:approved"}],
                "isCrossRepository": False,
                "headRepository": {"nameWithOwner": "VSCarpenter/Demo"},
                "baseRefName": "main",
            },
            {
                "number": 58,
                "title": "Unrelated human PR",
                "url": "https://github.com/vscarpenter/demo/pull/58",
                "headRefName": "fix/typo",
                "headRefOid": "b" * 40,
                "isDraft": False,
                "state": "OPEN",
                "labels": [],
                "isCrossRepository": False,
                "headRepository": {"nameWithOwner": "VSCarpenter/Demo"},
                "baseRefName": "main",
            },
        ]
    )
    runner = FakeRunner((payload, 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    prs = client.open_machinist_prs("agent/")

    assert runner.calls == [
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,url,headRefName,headRefOid,isDraft,state,labels,"
            "isCrossRepository,headRepository,baseRefName",
            "--repo",
            "vscarpenter/demo",
        ]
    ]
    assert prs == [
        PullRequest(
            number=57,
            title="Spec: Add dark mode (#42)",
            url="https://github.com/vscarpenter/demo/pull/57",
            branch="agent/issue-42",
            head_sha="a" * 40,
            is_draft=True,
            state="OPEN",
            labels=["machinist:approved"],
            is_cross_repository=False,
            head_repository="vscarpenter/demo",
            base="main",
        )
    ]


def test_open_machinist_prs_ignores_fork_branch_collision():
    payload = json.dumps(
        [
            {
                "number": 57,
                "title": "Fork collision",
                "url": "https://github.com/vscarpenter/demo/pull/57",
                "headRefName": "agent/issue-42",
                "headRefOid": "a" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [{"name": "machinist:approved"}],
                "isCrossRepository": True,
                "headRepository": {"nameWithOwner": "attacker/demo"},
                "baseRefName": "main",
            },
            {
                "number": 59,
                "title": "Same-repository task",
                "url": "https://github.com/vscarpenter/demo/pull/59",
                "headRefName": "agent/issue-42",
                "headRefOid": "b" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [],
                "isCrossRepository": False,
                "headRepository": {"nameWithOwner": "VSCarpenter/Demo"},
                "baseRefName": "main",
            },
        ]
    )
    client = GitHubClient(repo="vscarpenter/demo", runner=FakeRunner((payload, 0, "")))

    prs = client.open_machinist_prs("agent/")

    assert [pr.number for pr in prs] == [59]
    assert prs[0].is_cross_repository is False
    assert prs[0].head_repository == "vscarpenter/demo"


def test_pr_for_branch_searches_all_states_and_returns_exact_closed_match():
    payload = json.dumps(
        [
            {
                "number": 57,
                "title": "Spec: Add dark mode (#42)",
                "url": "https://github.com/vscarpenter/demo/pull/57",
                "headRefName": "agent/issue-42",
                "headRefOid": "a" * 40,
                "isDraft": False,
                "state": "CLOSED",
                "labels": [],
                "isCrossRepository": False,
                "headRepository": {"nameWithOwner": "VSCarpenter/Demo"},
                "baseRefName": "main",
            }
        ]
    )
    runner = FakeRunner((payload, 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    found = client.pr_for_branch("agent/issue-42")

    assert runner.calls == [
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            "agent/issue-42",
            "--limit",
            "1000",
            "--json",
            "number,title,url,headRefName,headRefOid,isDraft,state,labels,"
            "isCrossRepository,headRepository,baseRefName",
            "--repo",
            "vscarpenter/demo",
        ]
    ]
    assert found is not None
    assert found.number == 57
    assert found.state == "CLOSED"


def test_pr_for_branch_returns_none_when_exact_branch_is_absent():
    payload = json.dumps(
        [
            {
                "number": 58,
                "title": "Other",
                "url": "https://github.com/vscarpenter/demo/pull/58",
                "headRefName": "agent/issue-420",
                "headRefOid": "b" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [],
                "isCrossRepository": False,
                "headRepository": {"nameWithOwner": "VSCarpenter/Demo"},
                "baseRefName": "main",
            }
        ]
    )
    client = GitHubClient(runner=FakeRunner((payload, 0, "")))

    assert client.pr_for_branch("agent/issue-42") is None


def test_pr_for_branch_ignores_exact_named_fork_pr():
    payload = json.dumps(
        [
            {
                "number": 99,
                "title": "Fork collision",
                "url": "https://github.com/vscarpenter/demo/pull/99",
                "headRefName": "agent/issue-42",
                "headRefOid": "a" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [{"name": "machinist:approved"}],
                "isCrossRepository": True,
                "headRepository": {"nameWithOwner": "attacker/demo"},
                "baseRefName": "main",
            }
        ]
    )
    client = GitHubClient(repo="vscarpenter/demo", runner=FakeRunner((payload, 0, "")))

    assert client.pr_for_branch("agent/issue-42") is None


def test_pr_for_branch_ignores_mismatched_head_repository_even_if_flag_is_false():
    payload = json.dumps(
        [
            {
                "number": 99,
                "title": "Inconsistent repository identity",
                "url": "https://github.com/vscarpenter/demo/pull/99",
                "headRefName": "agent/issue-42",
                "headRefOid": "a" * 40,
                "isDraft": True,
                "state": "OPEN",
                "labels": [],
                "isCrossRepository": False,
                "headRepository": {"nameWithOwner": "attacker/demo"},
                "baseRefName": "main",
            }
        ]
    )
    client = GitHubClient(repo="vscarpenter/demo", runner=FakeRunner((payload, 0, "")))

    assert client.pr_for_branch("agent/issue-42") is None


@pytest.mark.parametrize(
    ("field", "malformed"),
    [
        ("isCrossRepository", _MISSING),
        ("isCrossRepository", None),
        ("isCrossRepository", "false"),
        ("headRepository", _MISSING),
        ("headRepository", None),
        ("headRepository", {"name": "demo"}),
        ("baseRefName", _MISSING),
        ("baseRefName", ""),
    ],
)
def test_pr_metadata_fails_closed_when_custody_fields_are_malformed(field, malformed):
    item = {
        "number": 57,
        "title": "Spec: Add dark mode (#42)",
        "url": "https://github.com/vscarpenter/demo/pull/57",
        "headRefName": "agent/issue-42",
        "headRefOid": "a" * 40,
        "isDraft": True,
        "state": "OPEN",
        "labels": [],
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": "VSCarpenter/Demo"},
        "baseRefName": "main",
    }
    if malformed is _MISSING:
        item.pop(field)
    else:
        item[field] = malformed
    client = GitHubClient(
        repo="vscarpenter/demo",
        runner=FakeRunner((json.dumps([item]), 0, "")),
    )

    with pytest.raises(GitHubError, match=field):
        client.open_machinist_prs("agent/")


def test_reopen_update_close_and_remove_pr_label_build_safe_argv():
    runner = FakeRunner(("", 0, ""), ("", 0, ""), ("", 0, ""), ("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    client.reopen_pr(57)
    client.update_pr(57, title="Revised spec", body="Updated body")
    client.remove_pr_label(57, "machinist:approved")
    client.close_pr(57)

    assert runner.calls == [
        ["gh", "pr", "reopen", "57", "--repo", "vscarpenter/demo"],
        [
            "gh",
            "pr",
            "edit",
            "57",
            "--title",
            "Revised spec",
            "--body",
            "Updated body",
            "--repo",
            "vscarpenter/demo",
        ],
        [
            "gh",
            "pr",
            "edit",
            "57",
            "--remove-label",
            "machinist:approved",
            "--repo",
            "vscarpenter/demo",
        ],
        ["gh", "pr", "close", "57", "--repo", "vscarpenter/demo"],
    ]


def test_update_pr_requires_at_least_one_change():
    client = GitHubClient(runner=FakeRunner())

    with pytest.raises(ValueError, match="title or body"):
        client.update_pr(57)


def test_upsert_pr_comment_posts_then_updates_bounded_markdown():
    runner = FakeRunner(
        (json.dumps({"id": 123}), 0, ""), (json.dumps({"id": 123}), 0, "")
    )
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)
    oversized = "x" * 20_000

    comment_id = client.upsert_pr_comment(57, oversized)
    updated_id = client.upsert_pr_comment(57, "short report", comment_id=comment_id)

    assert comment_id == updated_id == 123
    post = runner.calls[0]
    assert post[:6] == [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/vscarpenter/demo/issues/57/comments",
        "-f",
    ]
    assert post[6].startswith("body=")
    assert len(post[6].removeprefix("body=")) <= 16_000
    assert "truncated by AgentMachinist" in post[6]
    assert runner.calls[1] == [
        "gh",
        "api",
        "--method",
        "PATCH",
        "repos/vscarpenter/demo/issues/comments/123",
        "-f",
        "body=short report",
    ]


def test_approval_sha_returns_latest_valid_marker():
    payload = json.dumps(
        {
            "comments": [
                {
                    "body": "<!-- agentmachinist:approval sha=" + "a" * 40 + " -->",
                    "authorAssociation": "OWNER",
                    "author": {"login": "owner"},
                },
                {"body": "ordinary discussion"},
                {
                    "body": "<!-- agentmachinist:approval sha=" + "b" * 40 + " -->",
                    "authorAssociation": "NONE",
                    "author": {"login": "github-actions"},
                },
            ]
        }
    )
    runner = FakeRunner((payload, 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    assert client.approval_sha(57) == "b" * 40


def test_approval_sha_reads_a_marker_that_records_the_approver():
    """The workflow names the approver alongside the machine-readable marker."""
    payload = json.dumps(
        {
            "comments": [
                {
                    "body": (
                        "Approved by @someone for `" + "c" * 40 + "`. "
                        "<!-- agentmachinist:approval sha=" + "c" * 40 + " -->"
                    ),
                    "authorAssociation": "NONE",
                    "author": {"login": "github-actions"},
                }
            ]
        }
    )
    runner = FakeRunner((payload, 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    assert client.approval_sha(57) == "c" * 40


def test_approval_sha_ignores_markers_from_human_commenters():
    """Only the managed workflow mints Evidence; a human-typed marker is not
    Approval, whatever the author's repository association."""
    payload = json.dumps(
        {
            "comments": [
                {
                    "body": "<!-- agentmachinist:approval sha=" + "a" * 40 + " -->",
                    "authorAssociation": "OWNER",
                    "author": {"login": "owner"},
                },
                {
                    "body": "<!-- agentmachinist:approval sha=" + "c" * 40 + " -->",
                    "authorAssociation": "COLLABORATOR",
                    "author": {"login": "triage-collaborator"},
                },
                {
                    "body": "<!-- agentmachinist:approval sha=" + "b" * 40 + " -->",
                    "authorAssociation": "NONE",
                    "author": {"login": "drive-by-user"},
                },
            ]
        }
    )
    client = GitHubClient(runner=FakeRunner((payload, 0, "")))

    assert client.approval_sha(57) is None


def test_approval_sha_accepts_the_workflow_bot_login():
    payload = json.dumps(
        {
            "comments": [
                {
                    "body": "<!-- agentmachinist:approval sha=" + "d" * 40 + " -->",
                    "authorAssociation": "NONE",
                    "author": {"login": "github-actions[bot]"},
                }
            ]
        }
    )
    client = GitHubClient(runner=FakeRunner((payload, 0, "")))

    assert client.approval_sha(57) == "d" * 40


def test_approve_pr_requests_one_server_side_sha_bound_transaction():
    runner = FakeRunner(("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    client.approve_pr(57, head_sha="a" * 40)

    assert runner.calls[0][:5] == ["gh", "pr", "comment", "57", "--body"]
    assert runner.calls[0][5] == f"/machinist-execute {'a' * 40}"
    assert len(runner.calls) == 1


def test_label_names_returns_the_complete_label_set():
    runner = FakeRunner(
        (
            json.dumps([{"name": "agent-task"}, {"name": "machinist:approved"}]),
            0,
            "",
        )
    )
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    assert client.label_names() == {"agent-task", "machinist:approved"}
    assert "1000" in runner.calls[0]


def test_invalid_gh_json_is_a_github_error():
    client = GitHubClient(runner=FakeRunner(("not json", 0, "")))

    with pytest.raises(GitHubError, match="invalid JSON"):
        client.get_issue(42)


def test_default_branch_reads_repo_view():
    runner = FakeRunner((json.dumps({"defaultBranchRef": {"name": "trunk"}}), 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    assert client.default_branch() == "trunk"
    assert runner.calls == [
        [
            "gh",
            "repo",
            "view",
            "vscarpenter/demo",
            "--json",
            "defaultBranchRef",
        ]
    ]


def test_mark_ready_builds_argv():
    runner = FakeRunner(("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    client.mark_ready(57)

    assert runner.calls == [["gh", "pr", "ready", "57", "--repo", "vscarpenter/demo"]]


def test_gh_failure_raises_github_error_with_stderr():
    runner = FakeRunner(("", 1, "gh: Not Found (HTTP 404)"))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    with pytest.raises(GitHubError, match="Not Found"):
        client.get_issue(9999)


def test_missing_gh_binary_is_a_github_error():
    runner = FakeRunner(FileNotFoundError("gh"))
    client = GitHubClient(runner=runner)

    with pytest.raises(GitHubError, match="gh"):
        client.get_issue(1)

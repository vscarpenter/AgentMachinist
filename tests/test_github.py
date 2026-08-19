"""Tests for the gh-CLI-backed GitHub wrapper."""

import json
import subprocess

import pytest

from machinist.github import DraftPR, GitHubClient, GitHubError, Issue, PullRequest


class FakeRunner:
    """Records gh invocations and replays canned results."""

    def __init__(self, *results):
        self.calls = []
        self._results = list(results)

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
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
            "number,title,url,headRefName,headRefOid,isDraft,state,labels",
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
        )
    ]


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
            "number,title,url,headRefName,headRefOid,isDraft,state,labels",
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
            }
        ]
    )
    client = GitHubClient(runner=FakeRunner((payload, 0, "")))

    assert client.pr_for_branch("agent/issue-42") is None


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


def test_approval_sha_ignores_markers_from_untrusted_commenters():
    payload = json.dumps(
        {
            "comments": [
                {
                    "body": "<!-- agentmachinist:approval sha=" + "a" * 40 + " -->",
                    "authorAssociation": "MEMBER",
                    "author": {"login": "maintainer"},
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

    assert client.approval_sha(57) == "a" * 40


def test_approve_pr_records_sha_before_applying_label():
    runner = FakeRunner(("", 0, ""), ("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    client.approve_pr(57, label="machinist:approved", head_sha="a" * 40)

    assert runner.calls[0][:5] == ["gh", "pr", "comment", "57", "--body"]
    assert "a" * 40 in runner.calls[0][5]
    assert runner.calls[1][:5] == ["gh", "pr", "edit", "57", "--add-label"]


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
            "--json",
            "defaultBranchRef",
            "--repo",
            "vscarpenter/demo",
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

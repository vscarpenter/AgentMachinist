"""Tests for the gh-CLI-backed GitHub wrapper."""

import json
import subprocess

import pytest

from machinist.github import DraftPR, GitHubClient, GitHubError, Issue


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
            "gh", "issue", "view", "42",
            "--json", "number,title,body,url,labels",
            "--repo", "vscarpenter/demo",
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
            "gh", "pr", "create", "--draft",
            "--head", "agent/issue-42",
            "--base", "main",
            "--title", "Spec: Add dark mode",
            "--body", "Closes #42",
            "--repo", "vscarpenter/demo",
        ]
    ]
    assert pr == DraftPR(number=57, url=url)


def test_ensure_label_is_idempotent_via_force():
    runner = FakeRunner(("", 0, ""))
    client = GitHubClient(repo="vscarpenter/demo", runner=runner)

    client.ensure_label("machinist:approved", color="0e8a16", description="Spec approved")

    assert runner.calls == [
        [
            "gh", "label", "create", "machinist:approved",
            "--force",
            "--color", "0e8a16",
            "--description", "Spec approved",
            "--repo", "vscarpenter/demo",
        ]
    ]


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

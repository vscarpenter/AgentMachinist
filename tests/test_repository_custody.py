"""Contracts for exact controller repository and pull-request custody."""

import pytest

from machinist.config import MachinistConfig
from machinist.github import PullRequest
from machinist.repository_custody import (
    PullRequestExpectation,
    RepositoryCustodyError,
    bind_repository,
    verify_pull_request,
)


class Workspace:
    def __init__(self, target=("github.com", "Owner/Repo")):
        self.target = target

    def repository_target(self):
        return self.target


class GitHub:
    def __init__(self, repo=None, host=None):
        self.repo = repo
        self.repo_host = host
        self.bound = None

    def bind_repository(self, identity, *, hostname):
        self.bound = (hostname, identity)
        self.repo = identity
        self.repo_host = hostname


def pull_request(**changes):
    values = {
        "number": 57,
        "title": "Spec",
        "url": "https://github.com/owner/repo/pull/57",
        "branch": "agent/issue-42",
        "is_draft": True,
        "head_sha": "a" * 40,
        "state": "OPEN",
        "head_repository": "owner/repo",
        "base": "main",
    }
    values.update(changes)
    return PullRequest(**values)


def expectation() -> PullRequestExpectation:
    return PullRequestExpectation(
        number=57,
        branch="agent/issue-42",
        base="main",
        head_sha="a" * 40,
        repository="owner/repo",
        is_draft=True,
    )


def test_binding_normalizes_and_binds_one_exact_repository_target():
    github = GitHub()

    target = bind_repository(MachinistConfig(), github, Workspace())

    assert (target.host, target.identity) == ("github.com", "owner/repo")
    assert github.bound == ("github.com", "owner/repo")


def test_binding_rejects_configured_repository_mismatch():
    config = MachinistConfig.model_validate({"github": {"repo": "other/repo"}})

    with pytest.raises(RepositoryCustodyError, match="origin does not match"):
        bind_repository(config, GitHub(), Workspace())


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"head_sha": "b" * 40}, "head"),
        ({"base": "release"}, "base"),
        ({"state": "CLOSED"}, "state"),
        ({"is_cross_repository": True}, "cross-repository"),
        ({"head_repository": "attacker/repo"}, "repository"),
    ],
)
def test_exact_pr_verification_fails_closed(changes, message):
    with pytest.raises(RepositoryCustodyError, match=message):
        verify_pull_request(pull_request(**changes), expectation())


def test_exact_pr_verification_returns_the_observed_pr():
    observed = pull_request()

    assert verify_pull_request(observed, expectation()) is observed


def test_verify_branch_pr_accepts_this_repositorys_pr_at_branch_and_base():
    from machinist.repository_custody import verify_branch_pr

    verify_branch_pr(
        pull_request(), branch="agent/issue-42", base="main", repository="owner/repo"
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"branch": "agent/issue-99"}, r"branch 'agent/issue-99' != 'agent/issue-42'"),
        ({"base": "release"}, r"base 'release' != 'main'"),
        ({"is_cross_repository": True}, "cross-repository"),
        ({"head_repository": "fork/repo"}, "head repository does not match"),
    ],
)
def test_verify_branch_pr_rejects_foreign_or_misplaced_prs(changes, message):
    from machinist.repository_custody import verify_branch_pr

    with pytest.raises(RepositoryCustodyError, match=message):
        verify_branch_pr(
            pull_request(**changes),
            branch="agent/issue-42",
            base="main",
            repository="owner/repo",
        )

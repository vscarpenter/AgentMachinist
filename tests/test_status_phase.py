"""Tests for pipeline status classification."""

from machinist.config import MachinistConfig
from machinist.github import Issue, PullRequest
from machinist.phases.status import StatusRow, pipeline_status


def issue(number, title="An issue"):
    return Issue(number=number, title=title, body="", url=f"https://github.com/x/y/issues/{number}")


def pr(number, branch, *, draft=True, labels=(), title="Spec PR"):
    return PullRequest(
        number=number, title=title, url=f"https://github.com/x/y/pull/{number}",
        branch=branch, is_draft=draft, labels=list(labels),
    )


class FakeGitHub:
    def __init__(self, issues=(), prs=()):
        self._issues = list(issues)
        self._prs = list(prs)
        self.queries = []

    def issues_with_label(self, label):
        self.queries.append(("issues", label))
        return self._issues

    def open_machinist_prs(self, prefix):
        self.queries.append(("prs", prefix))
        return self._prs


def test_labeled_issue_without_pr_is_awaiting_spec():
    github = FakeGitHub(issues=[issue(3, "Fix frobnicator")])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows == [
        StatusRow(kind="issue", number=3, title="Fix frobnicator",
                  state="awaiting spec", url="https://github.com/x/y/issues/3")
    ]
    assert ("issues", "agent-task") in github.queries
    assert ("prs", "agent/") in github.queries


def test_issue_covered_by_spec_pr_is_not_double_listed():
    github = FakeGitHub(
        issues=[issue(42)],
        prs=[pr(57, "agent/issue-42")],
    )

    rows = pipeline_status(MachinistConfig(), github)

    assert [r.kind for r in rows] == ["pr"]


def test_draft_pr_without_label_awaits_approval():
    github = FakeGitHub(prs=[pr(57, "agent/issue-42")])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows[0].state == "awaiting approval"


def test_draft_pr_with_approved_label_is_approved():
    github = FakeGitHub(prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows[0].state == "approved"


def test_non_draft_pr_is_in_review():
    github = FakeGitHub(prs=[pr(57, "agent/issue-42", draft=False)])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows[0].state == "in review"


def test_custom_prefix_and_labels_come_from_config():
    config = MachinistConfig.model_validate(
        {
            "workspace": {"branch_prefix": "bot/"},
            "github": {"labels": {"trigger": "ai-task", "approved": "go"}},
        }
    )
    github = FakeGitHub(
        issues=[issue(42)],
        prs=[pr(57, "bot/issue-42", labels=["go"])],
    )

    rows = pipeline_status(config, github)

    assert [r.kind for r in rows] == ["pr"]
    assert rows[0].state == "approved"
    assert ("issues", "ai-task") in github.queries
    assert ("prs", "bot/") in github.queries


def test_no_activity_yields_empty_list():
    assert pipeline_status(MachinistConfig(), FakeGitHub()) == []

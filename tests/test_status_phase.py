"""Tests for pipeline status classification."""

from machinist.config import MachinistConfig
from machinist.github import Issue, PullRequest
from machinist.phases.status import StatusRow, pipeline_status
from machinist.lifecycle import Phase, TaskLifecycle


def issue(number, title="An issue"):
    return Issue(number=number, title=title, body="", url=f"https://github.com/x/y/issues/{number}")


def pr(number, branch, *, draft=True, labels=(), title="Spec PR", head_sha=None):
    return PullRequest(
        number=number, title=title, url=f"https://github.com/x/y/pull/{number}",
        branch=branch, head_sha=head_sha or ("a" * 40), is_draft=draft, labels=list(labels),
    )


class FakeGitHub:
    def __init__(self, issues=(), prs=(), approvals=None):
        self._issues = list(issues)
        self._prs = list(prs)
        self.queries = []
        self.approvals = approvals or {}

    def issues_with_label(self, label):
        self.queries.append(("issues", label))
        return self._issues

    def open_machinist_prs(self, prefix):
        self.queries.append(("prs", prefix))
        return self._prs

    def approval_sha(self, number):
        self.queries.append(("approval", number))
        return self.approvals.get(number)


def test_labeled_issue_without_pr_is_awaiting_spec():
    github = FakeGitHub(issues=[issue(3, "Fix frobnicator")])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows == [
        StatusRow(kind="issue", number=3, title="Fix frobnicator",
                  state="awaiting spec", url="https://github.com/x/y/issues/3",
                  issue_number=3)
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
    github = FakeGitHub(
        prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])],
        approvals={57: "a" * 40},
    )

    rows = pipeline_status(MachinistConfig(), github)

    assert rows[0].state == "approved"


def test_labeled_pr_without_sha_evidence_is_approval_pending():
    github = FakeGitHub(prs=[pr(57, "agent/issue-42", labels=["machinist:approved"])])

    assert pipeline_status(MachinistConfig(), github)[0].state == "approval pending"


def test_branch_change_after_approval_is_stale():
    github = FakeGitHub(
        prs=[pr(57, "agent/issue-42", labels=["machinist:approved"], head_sha="b" * 40)],
        approvals={57: "a" * 40},
    )

    assert pipeline_status(MachinistConfig(), github)[0].state == "approval stale"


def test_non_draft_pr_is_in_review():
    github = FakeGitHub(prs=[pr(57, "agent/issue-42", draft=False)])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows[0].state == "in review"


def test_implemented_pr_with_leftover_label_is_in_review_not_approved():
    # After Phase 3 the PR is ready-for-review but still carries the label;
    # draft-ness outranks the label so it must NOT look runnable.
    github = FakeGitHub(prs=[pr(57, "agent/issue-42", draft=False, labels=["machinist:approved"])])

    rows = pipeline_status(MachinistConfig(), github)

    assert rows[0].state == "in review"


def test_rows_carry_the_underlying_issue_number():
    github = FakeGitHub(
        issues=[issue(3)],
        prs=[pr(57, "agent/issue-42"), pr(58, "agent/weird-branch")],
    )

    rows = pipeline_status(MachinistConfig(), github)

    by_number = {r.number: r.issue_number for r in rows}
    assert by_number[3] == 3       # issue row: its own number
    assert by_number[57] == 42     # PR row: parsed from branch
    assert by_number[58] is None   # unparseable branch


def test_custom_prefix_and_labels_come_from_config():
    config = MachinistConfig.model_validate(
        {
            "workspace": {"branch_prefix": "bot/"},
            "github": {"labels": {"trigger": "ai-task", "approved": "go"}},
        }
    )
    github = FakeGitHub(
        issues=[issue(42)],
        prs=[pr(57, "bot/issue-42", labels=["go"])], approvals={57: "a" * 40},
    )

    rows = pipeline_status(config, github)

    assert [r.kind for r in rows] == ["pr"]
    assert rows[0].state == "approved"
    assert ("issues", "ai-task") in github.queries
    assert ("prs", "bot/") in github.queries


def test_no_activity_yields_empty_list():
    assert pipeline_status(MachinistConfig(), FakeGitHub()) == []


def test_durable_failed_run_is_visible_in_status(tmp_path):
    lifecycle = TaskLifecycle(tmp_path / "runs")
    try:
        lifecycle.run(42, Phase.EXECUTE, lambda claim: (_ for _ in ()).throw(RuntimeError("boom")))
    except RuntimeError:
        pass
    github = FakeGitHub(prs=[pr(57, "agent/issue-42")])

    row = pipeline_status(MachinistConfig(), github, lifecycle=lifecycle)[0]

    assert row.state == "execute failed"

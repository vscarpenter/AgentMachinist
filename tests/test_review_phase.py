"""Independent Review Phase orchestration and report-contract tests."""

from dataclasses import replace

import pytest

from machinist.config import MachinistConfig
from machinist.github import Issue, PullRequest
from machinist.phases.review import (
    ReviewPhaseError,
    parse_review_report,
    run_review_phase,
)


def make_pr(*, head_sha: str = "c" * 40, draft: bool = True) -> PullRequest:
    return PullRequest(
        number=57,
        title="Spec: Improve adoption (#42)",
        url="https://github.com/x/y/pull/57",
        branch="agent/issue-42",
        head_sha=head_sha,
        is_draft=draft,
        labels=["machinist:approved"],
        base="main",
    )


class FakeGitHub:
    def __init__(self, pr: PullRequest | None = None):
        self.pr = pr or make_pr()
        self.calls: list[tuple] = []
        self.ready = False

    def open_machinist_prs(self, prefix):
        self.calls.append(("open_machinist_prs", prefix))
        return [self.pr]

    def pr_for_branch(self, branch):
        self.calls.append(("pr_for_branch", branch))
        return replace(self.pr, is_draft=False if self.ready else self.pr.is_draft)

    def default_branch(self):
        return "main"

    def get_issue(self, number):
        return Issue(
            number=number,
            title="Improve adoption",
            body="## Acceptance criteria\n- [ ] Clear next action",
            url="https://github.com/x/y/issues/42",
        )

    def upsert_pr_comment(self, number, body, *, comment_id=None):
        self.calls.append(("upsert_pr_comment", number, body, comment_id))
        return comment_id or 700

    def mark_ready(self, number):
        self.calls.append(("mark_ready", number))
        self.ready = True


class FakeWorkspace:
    def __init__(self, tmp_path):
        self.path = tmp_path / "review"
        self.calls: list[tuple] = []
        self.dirty = False

    def provision_preview(self, task, branch, base_ref):
        self.calls.append(("provision_preview", task, branch, base_ref))
        spec = self.path / ".machinist/specs/issue-42-spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("## Approved spec\nKeep the CLI clear.\n")
        return self.path

    def assert_head(self, path, sha):
        self.calls.append(("assert_head", sha))

    def diff_against(self, path, base_ref, *, max_bytes):
        self.calls.append(("diff_against", base_ref, max_bytes))
        return "diff --git a/cli.py b/cli.py\n+clear next action\n"

    def has_changes(self, path):
        return self.dirty

    def cleanup_preview(self, path):
        self.calls.append(("cleanup_preview", path))


class FakeHarness:
    name = "reviewer"

    def __init__(self, output=None, *, on_review=None):
        self.output = output or (
            '{"version":1,"summary":"Meets the approved spec",'
            '"findings":[{"severity":"medium","confidence":"high",'
            '"file":"cli.py","line":12,"requirement":"Clear next action",'
            '"message":"Recovery copy can be more specific",'
            '"remediation":"Name the retry command"}]}'
        )
        self.on_review = on_review
        self.prompts: list[str] = []

    def review(self, prompt, cwd):
        self.prompts.append(prompt)
        if self.on_review:
            self.on_review()
        return self.output


class FakeClaim:
    attempt = 1

    def __init__(self):
        self.evidence = {}

    def checkpoint(self, **evidence):
        self.evidence.update(evidence)


def config() -> MachinistConfig:
    return MachinistConfig.model_validate({"review": {"enabled": True}})


def execute_evidence() -> dict[str, object]:
    return {
        "push_observed_sha": "c" * 40,
        "pr_base": "main",
        "verification_report": {"success": True, "gates": []},
        "change_summary": {"file_count": 1, "bytes": 18},
    }


def test_review_parses_structured_findings_and_marks_exact_pr_ready(tmp_path):
    github = FakeGitHub()
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness()
    claim = FakeClaim()

    pr = run_review_phase(
        42,
        config(),
        github=github,
        harness=harness,
        workspace=workspace,
        execute_evidence=execute_evidence(),
        claim=claim,
    )

    assert pr.number == 57
    assert "Approved spec" in harness.prompts[0]
    assert "Clear next action" in harness.prompts[0]
    assert "diff --git" in harness.prompts[0]
    comment = next(call for call in github.calls if call[0] == "upsert_pr_comment")
    assert "Independent review" in comment[2]
    assert "Recovery copy can be more specific" in comment[2]
    assert ("mark_ready", 57) in github.calls
    assert claim.evidence["reviewed_sha"] == "c" * 40
    assert claim.evidence["harness"] == {
        "name": "reviewer",
        "model": None,
        "profile": "review",
        "structured_usage": False,
    }
    assert any(call[0] == "cleanup_preview" for call in workspace.calls)


@pytest.mark.parametrize(
    "payload, message",
    [
        ("not json", "valid JSON"),
        ('{"version":2,"summary":"x","findings":[]}', "version"),
        (
            '{"version":1,"summary":"x","findings":['
            '{"severity":"urgent","confidence":"high","file":"x.py",'
            '"line":1,"requirement":"x","message":"x","remediation":"x"}]}',
            "severity",
        ),
        (
            '{"version":1,"summary":"x","findings":['
            '{"severity":"high","confidence":"high","file":"../x.py",'
            '"line":1,"requirement":"x","message":"x","remediation":"x"}]}',
            "repository-relative",
        ),
    ],
)
def test_review_report_contract_fails_closed(payload, message):
    with pytest.raises(ReviewPhaseError, match=message):
        parse_review_report(payload)


def test_review_rejects_changed_head_before_invoking_harness(tmp_path):
    github = FakeGitHub(make_pr(head_sha="d" * 40))
    harness = FakeHarness()

    with pytest.raises(ReviewPhaseError, match="changed after Execute"):
        run_review_phase(
            42,
            config(),
            github=github,
            harness=harness,
            workspace=FakeWorkspace(tmp_path),
            execute_evidence=execute_evidence(),
        )

    assert harness.prompts == []
    assert not any(call[0] == "mark_ready" for call in github.calls)


def test_review_rejects_harness_writes_and_always_cleans_preview(tmp_path):
    workspace = FakeWorkspace(tmp_path)
    harness = FakeHarness(on_review=lambda: setattr(workspace, "dirty", True))
    github = FakeGitHub()

    with pytest.raises(ReviewPhaseError, match="modified the read-only"):
        run_review_phase(
            42,
            config(),
            github=github,
            harness=harness,
            workspace=workspace,
            execute_evidence=execute_evidence(),
        )

    assert not any(call[0] == "mark_ready" for call in github.calls)
    assert any(call[0] == "cleanup_preview" for call in workspace.calls)

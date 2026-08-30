"""Side-effect-free Task policy explanation."""

from machinist.cancellation import CancellationStore
from machinist.config import MachinistConfig
from machinist.explain import explain_task
from machinist.github import PullRequest
from machinist.lifecycle import Phase, TaskLifecycle


class FakeGitHub:
    repo = "x/y"

    def issues_with_label(self, label):
        return []

    def open_machinist_prs(self, prefix):
        return [
            PullRequest(
                number=57,
                title="Spec: explain Tasks (#42)",
                url="https://github.com/x/y/pull/57",
                branch="agent/issue-42",
                is_draft=True,
                head_sha="c" * 40,
                labels=["machinist:approved"],
            )
        ]

    def approval_sha(self, number):
        return "c" * 40


def test_explain_reports_effective_policy_state_and_recovery_without_values(tmp_path):
    config = MachinistConfig.model_validate(
        {
            "review": {"enabled": True},
            "harness": {
                "name": "codex",
                "model": "implementation-model",
                "review": {"name": "claude-code", "model": "review-model"},
            },
            "tests": {"command": "pytest -q"},
            "github": {"spec_secret_env": "OPENAI_API_KEY"},
        }
    )
    lifecycle = TaskLifecycle(tmp_path / "runs")
    lifecycle.run(
        42,
        Phase.EXECUTE,
        lambda claim: claim.checkpoint(
            push_observed_sha="c" * 40,
            harness={"name": "codex", "model": "implementation-model"},
        ),
    )

    explanation = explain_task(
        42,
        config,
        FakeGitHub(),
        lifecycle=lifecycle,
        cancellation=CancellationStore(tmp_path / "runs"),
    ).to_dict()

    assert explanation["state"] == "awaiting review"
    assert explanation["next_action"] == "machinist review 42"
    assert explanation["profiles"]["execute"]["harness"] == "codex"
    assert explanation["profiles"]["review"]["model"] == "review-model"
    assert explanation["verification"][0]["command"] == "pytest -q"
    assert "OPENAI_API_KEY" in explanation["credentials"]["allowed_names"]
    serialized = str(explanation)
    assert "sk-" not in serialized
    assert explanation["attempts"]["execute"]["status"] == "succeeded"


def test_explain_rejects_issue_not_in_open_pipeline(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="not represented"):
        explain_task(
            99,
            MachinistConfig(),
            FakeGitHub(),
            lifecycle=TaskLifecycle(tmp_path / "runs"),
            cancellation=CancellationStore(tmp_path / "runs"),
        )

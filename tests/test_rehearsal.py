"""Local lifecycle rehearsal never needs GitHub artifacts."""

import json

import pytest

from machinist.config import MachinistConfig
from machinist.rehearsal import (
    RehearsalError,
    run_harness_rehearsal,
    simulate_rehearsal,
)


class FakeHarness:
    name = "fake"

    def __init__(self, phase, calls, *, fail=False):
        self.phase = phase
        self.calls = calls
        self.fail = fail

    def generate_spec(self, prompt, cwd):
        self.calls.append(("spec", prompt, cwd))
        return "## Spec\nUpdate rehearsal.txt.\n"

    def implement(self, prompt, cwd):
        self.calls.append(("execute", prompt, cwd))
        if self.fail:
            raise RuntimeError("harness unavailable")
        (cwd / "rehearsal.txt").write_text("implemented\n")
        return "implemented"

    def review(self, prompt, cwd):
        self.calls.append(("review", prompt, cwd))
        return json.dumps({"version": 1, "summary": "looks good", "findings": []})


def test_simulation_lists_full_lifecycle_without_creating_workspace():
    result = simulate_rehearsal(review_enabled=True)

    assert result.harness_used is False
    assert result.workspace is None
    assert result.transitions[-2:] == ("review complete", "human merge pending")


def test_harness_rehearsal_uses_disposable_repo_and_all_enabled_phases(tmp_path):
    calls = []
    config = MachinistConfig.model_validate({"review": {"enabled": True}})

    result = run_harness_rehearsal(
        config,
        harness_factory=lambda phase: FakeHarness(phase, calls),
        temp_parent=tmp_path,
    )

    assert [call[0] for call in calls] == ["spec", "execute", "review"]
    assert result.harness_used is True
    assert result.workspace is None
    assert list(tmp_path.iterdir()) == []


def test_failed_harness_rehearsal_retains_disposable_repo_for_diagnosis(tmp_path):
    calls = []

    with pytest.raises(RehearsalError, match="harness unavailable") as raised:
        run_harness_rehearsal(
            MachinistConfig(),
            harness_factory=lambda phase: FakeHarness(
                phase, calls, fail=phase == "execute"
            ),
            temp_parent=tmp_path,
        )

    assert raised.value.workspace.is_dir()
    assert (raised.value.workspace / ".git").is_dir()

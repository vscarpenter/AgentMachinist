from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from machinist.config import MachinistConfig
from machinist.lifecycle import Phase
from machinist.local_inspection import (
    LocalInspectionError,
    build_local_inspection,
    render_local_inspection,
)
from machinist.local_workflow import LocalWorkflow


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeHarness:
    name = "fake"
    config = MachinistConfig().harness

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.summary = "Candidate matches the approved plan."
        self.spec = "# Plan\n\nReturn two from answer and retain its regression test.\n"
        self.fail = False

    def generate_spec(self, prompt: str, cwd: Path) -> str:
        self.calls.append("spec")
        return self.spec

    def implement(self, prompt: str, cwd: Path) -> str:
        self.calls.append("execute")
        if self.fail:
            raise RuntimeError("interrupted: API_KEY=sample-error-value")
        (cwd / "feature.py").write_text("def answer():\n    return 2\n")
        (cwd / "tests/test_feature.py").write_text(
            "import unittest\nfrom feature import answer\n"
            "class TestFeature(unittest.TestCase):\n"
            "    def test_answer(self): self.assertEqual(answer(), 2)\n"
        )
        return "Updated the answer and its regression test."

    def review(self, prompt: str, cwd: Path) -> str:
        self.calls.append("review")
        return json.dumps(
            {
                "version": 1,
                "summary": self.summary,
                "findings": [
                    {
                        "severity": "low",
                        "confidence": "high",
                        "file": "feature.py",
                        "line": 1,
                        "requirement": "Keep the change small.",
                        "message": "Consider a descriptive docstring.",
                        "remediation": "Explain the purpose of answer().",
                    }
                ],
            }
        )


@pytest.fixture
def local(tmp_path: Path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("maintenance.auto", "false"),
    ):
        git(root, "config", key, value)
    (root / ".gitignore").write_text("__pycache__/\n/.machinist/runs/\n")
    (root / "feature.py").write_text("def answer():\n    return 1\n")
    (root / "tests").mkdir()
    (root / "tests/test_feature.py").write_text(
        "import unittest\nfrom feature import answer\n"
        "class TestFeature(unittest.TestCase):\n"
        "    def test_answer(self): self.assertEqual(answer(), 1)\n"
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    config = MachinistConfig.model_validate(
        {
            "workspace": {"root": str(tmp_path / "workshops")},
            "github": {"spec_source": "local", "manage_workflows": False},
            "tests": {"command": f"{sys.executable} -m unittest discover -s tests"},
            "review": {"enabled": True},
        }
    )
    settings = root / ".machinist/runs/local/config.yaml"
    settings.parent.mkdir(parents=True)
    settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    harness = FakeHarness()
    workflow = LocalWorkflow(
        config, repo_root=root, harness_factory=lambda phase, number: harness
    )
    return root, workflow, harness


def delivered(local):
    root, workflow, harness = local
    task = workflow.start(
        "Return a better answer", "Return two, with a regression test."
    )
    task = workflow.approve(task.id, expected_sha=task.spec_sha)
    return root, workflow, harness, task


def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }


def test_awaiting_approval_shows_saved_plan_without_candidate_or_side_effects(local):
    root, workflow, harness = local
    task = workflow.start("Return a better answer")
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["task_id"] == task.id
    assert payload["state"] == "awaiting approval"
    assert payload["spec"]["sha"] == task.spec_sha
    assert payload["spec"]["verified"] is True
    assert "Return two" in payload["spec"]["text"]
    assert payload["candidate"]["sha"] is None
    assert payload["diff"] is None
    assert payload["review"] is None
    assert payload["next_action"] == (
        f"machinist approve --task {task.id} --spec-sha {task.spec_sha}"
    )
    assert "Saved plan" in text and task.spec_sha in text
    assert snapshot(root) == before
    assert harness.calls == ["spec"]


def test_reviewed_candidate_has_exact_diff_checks_findings_and_attempt_history(local):
    root, workflow, harness, task = delivered(local)
    before = snapshot(root)
    calls = harness.calls.copy()

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)
    as_json = json.loads(render_local_inspection(root, task.id, as_json=True))

    assert payload == as_json
    assert payload["objective"] == "Return two, with a regression test."
    assert payload["candidate"] == {
        "sha": task.candidate_sha,
        "verified": True,
        "reviewed": True,
    }
    assert payload["diff"]["base_sha"] == task.spec_sha
    assert payload["diff"]["candidate_sha"] == task.candidate_sha
    assert payload["diff"]["changed_files"] == ["feature.py", "tests/test_feature.py"]
    assert "+    return 2" in payload["diff"]["text"]
    assert payload["verification"]["success"] is True
    assert payload["verification"]["gates"][0]["required"] is True
    assert payload["verification"]["gates"][0]["status"] == "passed"
    assert payload["review"]["reviewed_sha"] == task.candidate_sha
    assert payload["review"]["findings"][0]["message"].startswith("Consider")
    assert payload["review"]["findings"][0]["confidence"] == "high"
    assert {item["phase"] for item in payload["history"]} == {
        "spec",
        "execute",
        "review",
    }
    assert payload["next_action"] == "machinist integrate T1"
    assert "Findings are advisory" in text
    assert "feature.py:1" in text
    assert "Check results" in text and "Attempt history" in text
    assert snapshot(root) == before
    assert harness.calls == calls


@pytest.mark.parametrize(
    "tamper", ["branch", "task_sha", "execute_evidence", "review_evidence"]
)
def test_changed_candidate_or_evidence_never_gets_an_actionable_delivery_summary(
    local, tamper
):
    root, workflow, _harness, task = delivered(local)
    if tamper == "branch":
        git(root, "update-ref", f"refs/heads/{task.branch}", task.base_sha)
    elif tamper == "task_sha":
        workflow.store.update(task, candidate_sha=task.base_sha)
    else:
        phase = "execute" if tamper == "execute_evidence" else "review"
        path = root / f".machinist/runs/local/issue-1-{phase}.json"
        record = json.loads(path.read_text())
        key = "implementation_sha" if phase == "execute" else "reviewed_sha"
        record["evidence"][key] = task.base_sha
        path.write_text(json.dumps(record))
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["issues"]
    assert payload["candidate"]["reviewed"] is False
    assert payload["state"] != "ready to integrate"
    assert payload["next_action"] != "machinist integrate T1"
    assert "Next: machinist integrate" not in text
    if tamper != "review_evidence":
        assert payload["diff"] is None
    assert snapshot(root) == before


def test_stored_findings_must_match_the_successful_exact_review_evidence(local):
    root, workflow, _harness, task = delivered(local)
    changed = {**task.review_report, "findings": []}
    workflow.store.update(task, review_report=changed)

    payload = build_local_inspection(root, task.id)

    assert payload["review"] is None
    assert payload["candidate"]["reviewed"] is False
    assert any("Review" in issue for issue in payload["issues"])
    assert payload["next_action"] != "machinist integrate T1"


@pytest.mark.parametrize("tamper", ["missing_name", "failed_required_gate"])
def test_malformed_or_contradictory_gate_evidence_does_not_claim_verified_delivery(
    local, tamper
):
    root, _workflow, _harness, task = delivered(local)
    path = root / ".machinist/runs/local/issue-1-execute.json"
    record = json.loads(path.read_text())
    gate = record["evidence"]["verification_report"]["gates"][0]
    if tamper == "missing_name":
        gate.pop("name")
    else:
        gate["status"] = "failed"
    path.write_text(json.dumps(record))

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["candidate"]["verified"] is False
    assert payload["issues"]
    assert payload["next_action"] != "machinist integrate T1"
    assert "Next: machinist integrate" not in text


def test_baseline_failure_explains_failed_checks_before_any_harness_work(local):
    root, workflow, harness = local
    workflow.config.tests.command = f"{sys.executable} -c 'raise SystemExit(3)'"
    with pytest.raises(Exception, match="verification gates blocked"):
        workflow.start("Return a better answer")
    before = snapshot(root)

    payload = build_local_inspection(root, "T1")
    text = render_local_inspection(root, "T1")

    assert payload["state"] == "baseline failed"
    assert payload["spec"]["sha"] is None
    assert payload["verification"]["phase"] == "baseline"
    assert payload["verification"]["success"] is False
    assert payload["verification"]["gates"][0]["status"] == "failed"
    assert payload["next_action"] == "machinist retry --task T1 --phase spec"
    assert "verification gates blocked" in text
    assert harness.calls == []
    assert snapshot(root) == before


def test_failed_execute_shows_its_actual_failed_checks_without_a_candidate(local):
    root, workflow, harness = local
    workflow.config.tests.command = (
        f"{sys.executable} -c 'from feature import answer; assert answer() == 1'"
    )
    task = workflow.start("Return a better answer")
    with pytest.raises(Exception, match="verification gates blocked"):
        workflow.approve(task.id, expected_sha=task.spec_sha)
    assert workflow.store.get(task.id).candidate_sha is None
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["state"] == "execute failed"
    assert payload["verification"]["phase"] == "execute"
    assert payload["verification"]["success"] is False
    assert payload["verification"]["gates"][0]["status"] == "failed"
    assert "AssertionError" in text
    assert payload["candidate"]["verified"] is False
    assert payload["diff"] is None
    assert payload["next_action"] == "machinist retry --task T1 --phase execute"
    assert harness.calls == ["spec", "execute"]
    assert snapshot(root) == before


def test_truncated_saved_plan_gives_an_exact_read_command_before_approval(
    local, monkeypatch
):
    from machinist import local_inspection

    root, workflow, harness = local
    task = workflow.start("Return a better answer")
    monkeypatch.setattr(local_inspection, "_MAX_SPEC_CHARS", 20)
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    selector = f"{task.spec_sha}:.machinist/specs/task-1-spec.md"
    assert payload["spec"]["truncated"] is True
    assert payload["spec"]["command"] == f"git show {selector}"
    assert git(root, "show", selector) == harness.spec.strip()
    assert "Read the complete plan before approving" in text
    assert f"Full plan: git show {selector}" in text
    assert payload["next_action"] == (
        f"machinist approve --task T1 --spec-sha {task.spec_sha}"
    )
    assert snapshot(root) == before
    assert harness.calls == ["spec"]


def test_spec_delivery_crash_preserves_saved_plan_and_the_supported_retry_action(
    local, monkeypatch
):
    root, workflow, harness = local
    original = workflow.store.update

    def interrupted(task, **changes):
        saved = original(task, **changes)
        if "spec_sha" in changes:
            raise RuntimeError("crash after saving Spec")
        return saved

    monkeypatch.setattr(workflow.store, "update", interrupted)
    with pytest.raises(RuntimeError, match="crash after saving Spec"):
        workflow.start("Return a better answer")
    monkeypatch.setattr(workflow.store, "update", original)
    task = workflow.store.get("T1")
    assert task.spec_sha
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["state"] == "spec failed"
    assert payload["spec"]["sha"] == task.spec_sha
    assert "Return two" in payload["spec"]["text"]
    assert payload["spec"]["verified"] is False
    assert payload["next_action"] == "machinist retry --task T1 --phase spec"
    assert "Next: machinist approve" not in text
    assert payload["history"][0]["spec_sha"] == task.spec_sha
    assert snapshot(root) == before
    assert harness.calls == ["spec"]
    recovered = workflow.retry(task.id, phase=Phase.SPEC)
    assert recovered.spec_sha == task.spec_sha
    assert harness.calls == ["spec"]


def test_revision_history_shows_each_exact_plan_and_bounded_sanitized_feedback(local):
    root, workflow, harness = local
    task = workflow.start("Return a better answer")
    first_sha = task.spec_sha
    harness.spec = (
        "# Plan\n\nReturn three from answer and retain its regression test.\n"
    )
    feedback = "Return three instead. API_KEY=sample-feedback-value\n" + "x" * 5_000
    revised = workflow.revise(task.id, feedback)
    before = snapshot(root)

    payload = build_local_inspection(root, revised.id)
    text = render_local_inspection(root, revised.id)

    spec_attempts = [item for item in payload["history"] if item["phase"] == "spec"]
    assert [item["spec_sha"] for item in spec_attempts] == [first_sha, revised.spec_sha]
    assert spec_attempts[0]["local_feedback"] is None
    assert "Return three instead" in spec_attempts[1]["local_feedback"]
    assert len(spec_attempts[1]["local_feedback"]) <= 1_000
    assert "sample-feedback-value" not in text
    assert "[REDACTED]" in text
    assert "Requested plan change" in text
    assert first_sha in text and revised.spec_sha in text
    assert snapshot(root) == before
    assert harness.calls == ["spec", "spec"]


def test_failed_execute_keeps_error_history_and_does_not_relaunch_any_work(local):
    root, workflow, harness = local
    task = workflow.start("Return a better answer")
    harness.fail = True
    with pytest.raises(RuntimeError, match="interrupted"):
        workflow.approve(task.id, expected_sha=task.spec_sha)
    calls = harness.calls.copy()
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["state"] == "execute failed"
    assert payload["next_action"] == "machinist retry --task T1 --phase execute"
    failed = next(item for item in payload["history"] if item["phase"] == "execute")
    assert "interrupted" in failed["error"]
    assert "sample-error-value" not in text
    assert "[REDACTED]" in text
    assert snapshot(root) == before
    assert harness.calls == calls


def test_corrupt_projection_is_visible_without_losing_valid_attempt_history(local):
    root, _workflow, _harness, task = delivered(local)
    path = root / ".machinist/runs/local/issue-1-review.json"
    path.write_text("{broken JSON")
    before = snapshot(root)

    payload = build_local_inspection(root, task.id)
    text = render_local_inspection(root, task.id)

    assert payload["corrupt"]
    assert any("issue-1-review.json" in item["path"] for item in payload["corrupt"])
    assert any(item["phase"] == "review" for item in payload["history"])
    assert payload["candidate"]["reviewed"] is False
    assert payload["next_action"] != "machinist integrate T1"
    assert "Corrupt" in text
    assert snapshot(root) == before


def test_text_and_json_are_bounded_and_remove_terminal_controls_and_recognized_secrets(
    local,
):
    root, workflow, harness = local
    harness.spec = (
        "# Plan\nAPI_KEY=sample-plan-value\n\x1b[31mReturn two\x1b[0m\n" + "x" * 40_000
    )
    harness.summary = (
        "Authorization: Bearer sample-review-value\n\x1b[31mReviewed\x1b[0m"
    )
    task = workflow.start(
        "Return a better answer", "API_KEY=sample-body-value\n" + "y" * 45_000
    )
    task = workflow.approve(task.id, expected_sha=task.spec_sha)

    text = render_local_inspection(root, task.id)
    encoded = render_local_inspection(root, task.id, as_json=True)
    payload = json.loads(encoded)

    for output in (text, encoded):
        assert "sample-plan-value" not in output
        assert "sample-review-value" not in output
        assert "sample-body-value" not in output
        assert "\x1b" not in output and "\\u001b" not in output
        assert "[REDACTED]" in output
        assert len(output) < 512_000
    assert payload["spec"]["truncated"] is True
    assert len(payload["spec"]["text"]) <= 20_000
    assert len(payload["objective"]) <= 4_000
    assert workflow.store.get(task.id).spec_sha == task.spec_sha


def test_oversized_diff_is_bounded_at_the_subprocess_and_never_certifies_delivery(
    local, monkeypatch
):
    from machinist import local_inspection

    root, _workflow, _harness, task = delivered(local)
    monkeypatch.setattr(local_inspection, "_MAX_DIFF_BYTES", 32)

    payload = build_local_inspection(root, task.id)

    assert payload["diff"] is None
    assert payload["issues"]
    assert payload["next_action"] != "machinist integrate T1"


def test_uses_no_harness_or_forge_clients_and_ignores_unrelated_evidence(
    local, monkeypatch
):
    import machinist.harness
    from machinist.github import GitHubClient

    root, _workflow, _harness, task = delivered(local)

    def forbidden(*args, **kwargs):
        raise AssertionError("inspection must not construct a model or forge client")

    monkeypatch.setattr(machinist.harness, "get_harness", forbidden)
    monkeypatch.setattr(GitHubClient, "__init__", forbidden)
    path = root / ".machinist/runs/local/issue-1-execute.json"
    record = json.loads(path.read_text())
    record["evidence"]["unrelated_private_payload"] = "do-not-render-this"
    path.write_text(json.dumps(record))

    output = render_local_inspection(root, task.id, as_json=True)

    assert "do-not-render-this" not in output
    assert json.loads(output)["candidate"]["reviewed"] is True


@pytest.mark.parametrize("task_id", ["T2", "1", "T0", "../../elsewhere"])
def test_invalid_or_missing_task_returns_an_actionable_inspection_error(local, task_id):
    root, _workflow, _harness = local

    with pytest.raises(LocalInspectionError, match="Task"):
        render_local_inspection(root, task_id)

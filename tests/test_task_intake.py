"""Managed task template and readiness linting."""

import pytest

from machinist.task_intake import (
    TaskTemplateDriftError,
    lint_task_body,
    render_task_body,
    sync_task_template,
)


def complete_body() -> str:
    return render_task_body(
        objective="Make failed authentication recovery obvious to a new operator.",
        acceptance="- [ ] Error names the missing credential\n- [ ] Output gives one next command",
        constraints="Preserve the local-first trust model.",
        verification="Run `uv run pytest tests/test_auth.py` and inspect CLI output.",
        context="Reported during first-run testing.",
    )


def test_rendered_task_body_round_trips_through_readiness_lint() -> None:
    report = lint_task_body(complete_body())

    assert report.ready is True
    assert report.errors == ()


def test_lint_names_missing_and_non_actionable_sections() -> None:
    body = """## Objective
TBD

## Acceptance criteria
It works

## Constraints
_No response_
"""

    report = lint_task_body(body)

    assert report.ready is False
    by_field = {finding.field: finding.message for finding in report.errors}
    assert "concrete outcome" in by_field["objective"]
    assert "checkbox" in by_field["acceptance criteria"]
    assert "verification" in by_field
    assert "replace placeholder" in by_field["constraints"]


def test_task_template_projection_is_sealed_and_refuses_user_content(tmp_path) -> None:
    first = sync_task_template(tmp_path, check=False)
    second = sync_task_template(tmp_path, check=False)
    target = tmp_path / ".github/ISSUE_TEMPLATE/agentmachinist-task.yml"

    assert first.written is True
    assert second.written is False
    text = target.read_text()
    assert text.startswith("# agentmachinist-managed-sha256: ")
    assert "id: objective" in text
    assert "id: acceptance" in text
    assert "labels: []" in text

    target.write_text("name: My issue form\n")
    with pytest.raises(TaskTemplateDriftError, match="unrecognized"):
        sync_task_template(tmp_path, check=False)
    assert target.read_text() == "name: My issue form\n"


def test_task_template_check_reports_drift_without_writing(tmp_path) -> None:
    target = tmp_path / ".github/ISSUE_TEMPLATE/agentmachinist-task.yml"

    with pytest.raises(
        TaskTemplateDriftError, match="run 'machinist task template --write'"
    ):
        sync_task_template(tmp_path, check=True)

    assert not target.exists()

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


def test_github_issue_form_heading_level_is_ready() -> None:
    assert lint_task_body(complete_body().replace("## ", "### ")).ready


@pytest.mark.parametrize("heading", ["##", "###"])
def test_child_headings_stay_in_their_parent_section(heading: str) -> None:
    body = complete_body().replace("## ", f"{heading} ")
    body = body.replace(
        f"{heading} Acceptance criteria\n",
        f"{heading} Acceptance criteria\n\n{heading}# Observable behavior\n",
    )

    assert lint_task_body(body).ready


def test_child_heading_cannot_supply_a_missing_required_section() -> None:
    body = complete_body().replace("## Verification", "### Verification")

    report = lint_task_body(body)

    assert not report.ready
    assert any(finding.field == "verification" for finding in report.errors)


@pytest.mark.parametrize("criterion", ["- [ ]", "- [x]   ", "- [ ] TBD"])
def test_empty_or_placeholder_acceptance_checkbox_is_rejected(criterion: str) -> None:
    body = render_task_body(
        objective="Make failed authentication recovery obvious to a new operator.",
        acceptance=criterion,
        constraints="Preserve the local-first trust model.",
        verification="Run the authentication regression suite.",
        context="Not provided",
    )

    report = lint_task_body(body)

    assert not report.ready
    assert any(finding.field == "acceptance criteria" for finding in report.errors)


def test_empty_checkbox_is_rejected_alongside_a_complete_criterion() -> None:
    body = complete_body().replace("## Constraints", "- [ ]\n\n## Constraints")

    assert not lint_task_body(body).ready


def test_objective_keeps_documented_six_word_minimum() -> None:
    body = complete_body().replace(
        "Make failed authentication recovery obvious to a new operator.",
        "Make auth errors actionable",
    )

    report = lint_task_body(body)

    assert not report.ready
    assert report.errors[0].field == "objective"
    assert "six words" in report.errors[0].message


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

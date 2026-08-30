"""Managed GitHub issue form and readiness checks for AgentMachinist Tasks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from machinist.managed_paths import read_managed_text, write_managed_text

TASK_TEMPLATE_PATH = Path(".github/ISSUE_TEMPLATE/agentmachinist-task.yml")
_TEMPLATE = files("machinist") / "templates/github/agentmachinist-task.yml"
_MANAGED_MARKER = "# agentmachinist-managed-sha256: "
_MAX_TEMPLATE_BYTES = 512 * 1024
_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_PLACEHOLDERS = {"", "tbd", "todo", "none", "n/a", "_no response_"}


class TaskTemplateDriftError(Exception):
    """The managed task template is absent, stale, or unrecognized."""


@dataclass(frozen=True)
class TaskTemplateSync:
    written: bool


@dataclass(frozen=True)
class TaskLintFinding:
    field: str
    message: str


@dataclass(frozen=True)
class TaskLintReport:
    errors: tuple[TaskLintFinding, ...]

    @property
    def ready(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "errors": [
                {"field": finding.field, "message": finding.message}
                for finding in self.errors
            ],
        }


def preflight_task_template(repo_root: Path) -> None:
    """Reject unsafe or user-owned template content before setup mutates files."""
    current = read_managed_text(
        repo_root,
        TASK_TEMPLATE_PATH,
        max_bytes=_MAX_TEMPLATE_BYTES,
    )
    if current is not None and not _is_pristine(current):
        raise TaskTemplateDriftError(
            "refusing to replace unrecognized or modified task template; "
            "review and remove or restore it explicitly"
        )


def sync_task_template(repo_root: Path, *, check: bool) -> TaskTemplateSync:
    """Project the sealed issue form without overwriting user-owned content."""
    payload = _TEMPLATE.read_text()
    wanted = _seal(payload)
    current = read_managed_text(
        repo_root,
        TASK_TEMPLATE_PATH,
        max_bytes=_MAX_TEMPLATE_BYTES,
    )
    if current == wanted:
        return TaskTemplateSync(written=False)
    if current is not None and not _is_pristine(current):
        preflight_task_template(repo_root)
    if check:
        raise TaskTemplateDriftError(
            "managed task template drift; run 'machinist task template --write'"
        )
    write_managed_text(repo_root, TASK_TEMPLATE_PATH, wanted)
    return TaskTemplateSync(written=True)


def render_task_body(
    *,
    objective: str,
    acceptance: str,
    constraints: str,
    verification: str,
    context: str,
) -> str:
    """Render the canonical Markdown body consumed by readiness linting."""
    sections = (
        ("Objective", objective),
        ("Acceptance criteria", acceptance),
        ("Constraints", constraints),
        ("Verification", verification),
        ("Context", context),
    )
    return (
        "\n\n".join(
            f"## {heading}\n{content.strip()}" for heading, content in sections
        ).rstrip()
        + "\n"
    )


def lint_task_body(body: str) -> TaskLintReport:
    """Return actionable readiness errors for one Task body."""
    sections = _sections(body)
    errors: list[TaskLintFinding] = []
    objective = sections.get("objective")
    if _placeholder(objective) or len((objective or "").split()) < 6:
        errors.append(
            TaskLintFinding(
                "objective",
                "describe a concrete outcome in at least six words",
            )
        )
    acceptance = sections.get("acceptance criteria")
    if (
        _placeholder(acceptance)
        or re.search(r"(?m)^\s*-\s*\[[ xX]\]", acceptance or "") is None
    ):
        errors.append(
            TaskLintFinding(
                "acceptance criteria",
                "add at least one Markdown checkbox with an observable result",
            )
        )
    for field in ("constraints", "verification"):
        value = sections.get(field)
        if value is None:
            errors.append(TaskLintFinding(field, f"add the {field} section"))
        elif _placeholder(value):
            errors.append(
                TaskLintFinding(
                    field, "replace placeholder text with actionable detail"
                )
            )
    return TaskLintReport(tuple(errors))


def _sections(body: str) -> dict[str, str]:
    matches = list(_SECTION_PATTERN.finditer(body))
    return {
        match.group(1).strip().casefold(): body[
            match.end() : matches[index + 1].start()
            if index + 1 < len(matches)
            else None
        ].strip()
        for index, match in enumerate(matches)
    }


def _placeholder(value: str | None) -> bool:
    return value is None or value.strip().casefold() in _PLACEHOLDERS


def _seal(payload: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_MANAGED_MARKER}{digest}\n{payload}"


def _is_pristine(content: str) -> bool:
    marker, separator, payload = content.partition("\n")
    if not separator or not marker.startswith(_MANAGED_MARKER):
        return False
    supplied = marker.removeprefix(_MANAGED_MARKER)
    if len(supplied) != 64 or any(ch not in "0123456789abcdef" for ch in supplied):
        return False
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == supplied

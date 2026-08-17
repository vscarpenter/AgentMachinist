"""Deterministic projection of machinist.yaml into managed Actions workflows."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from machinist.config import MachinistConfig, SpecSource

_TEMPLATES = files("machinist") / "templates" / "github"
_MANAGED = ("machinist-spec.yml", "machinist-approve.yml")


class WorkflowDriftError(Exception):
    """Managed workflows do not match the active configuration."""


@dataclass(frozen=True)
class WorkflowSyncReport:
    written: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


def expected_workflows(
    config: MachinistConfig, *, installed_version: str
) -> dict[str, str]:
    """Render the exact managed files for a configuration."""
    approval = (_TEMPLATES / "machinist-approve.yml").read_text().replace(
        "__APPROVED_LABEL__", config.github.labels.approved
    )
    rendered = {"machinist-approve.yml": approval}
    if config.github.spec_source is SpecSource.GITHUB_ACTIONS:
        spec = (_TEMPLATES / "machinist-spec.yml").read_text()
        spec = spec.replace("__TRIGGER_LABEL__", config.github.labels.trigger)
        spec = spec.replace("__VERSION__", installed_version)
        rendered["machinist-spec.yml"] = spec
    return rendered


def sync_workflows(
    repo_root: Path,
    config: MachinistConfig,
    *,
    installed_version: str,
    check: bool,
) -> WorkflowSyncReport:
    """Check or write managed workflows; never touch unrelated workflow files."""
    root = Path(repo_root)
    directory = root / ".github" / "workflows"
    expected = expected_workflows(config, installed_version=installed_version)
    drift: list[str] = []
    for name in _MANAGED:
        path = directory / name
        wanted = expected.get(name)
        if wanted is None:
            if path.exists():
                drift.append(name)
        elif not path.exists() or path.read_text() != wanted:
            drift.append(name)

    if check:
        if drift:
            raise WorkflowDriftError(
                "managed workflow drift: " + ", ".join(sorted(drift))
                + "; run 'machinist sync-workflows'"
            )
        return WorkflowSyncReport()

    written: list[str] = []
    removed: list[str] = []
    if expected:
        directory.mkdir(parents=True, exist_ok=True)
    for name in _MANAGED:
        path = directory / name
        wanted = expected.get(name)
        if wanted is None:
            if path.exists():
                path.unlink()
                removed.append(name)
        elif not path.exists() or path.read_text() != wanted:
            path.write_text(wanted)
            written.append(name)
    return WorkflowSyncReport(tuple(written), tuple(removed))

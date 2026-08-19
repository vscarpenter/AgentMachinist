"""Deterministic projection of machinist.yaml into managed Actions workflows."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from machinist.config import MachinistConfig, SpecInstall, SpecSource
from machinist.managed_paths import (
    read_managed_text,
    remove_managed_file,
    write_managed_text,
)

_TEMPLATES = files("machinist") / "templates" / "github"
_MANAGED = ("machinist-spec.yml", "machinist-approve.yml")
_MANAGED_MARKER = "# agentmachinist-managed-sha256: "
_MAX_WORKFLOW_BYTES = 2 * 1024 * 1024


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
    if not config.github.manage_workflows:
        return {}
    return {
        name: _seal_managed_workflow(payload)
        for name, payload in _render_workflow_payloads(
            config, installed_version=installed_version
        ).items()
    }


def _render_workflow_payloads(
    config: MachinistConfig, *, installed_version: str
) -> dict[str, str]:
    """Render workflow bodies before the self-authenticating ownership marker."""
    approval = (
        (_TEMPLATES / "machinist-approve.yml")
        .read_text()
        .replace("__APPROVED_LABEL__", config.github.labels.approved)
    )
    rendered = {"machinist-approve.yml": approval}
    if config.github.spec_source is SpecSource.GITHUB_ACTIONS:
        spec = (_TEMPLATES / "machinist-spec.yml").read_text()
        spec = spec.replace("__TRIGGER_LABEL__", config.github.labels.trigger)
        if config.github.spec_install is SpecInstall.CHECKOUT:
            spec = spec.replace("__INSTALL_AGENTMACHINIST__", "uv sync --frozen")
            spec = spec.replace("__SPEC_INVOKE__", "uv run machinist spec")
        else:
            spec = spec.replace(
                "__INSTALL_AGENTMACHINIST__",
                f"uv tool install agentmachinist=={installed_version}",
            )
            spec = spec.replace("__SPEC_INVOKE__", "machinist spec")
        rendered["machinist-spec.yml"] = spec
    return rendered


def _seal_managed_workflow(payload: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_MANAGED_MARKER}{digest}\n{payload}"


def _is_pristine_managed_workflow(content: str) -> bool:
    marker, separator, payload = content.partition("\n")
    if not separator or not marker.startswith(_MANAGED_MARKER):
        return False
    supplied = marker.removeprefix(_MANAGED_MARKER)
    if len(supplied) != 64 or any(ch not in "0123456789abcdef" for ch in supplied):
        return False
    return hashlib.sha256(payload.encode("utf-8")).hexdigest() == supplied


def preflight_workflow_paths(repo_root: Path) -> None:
    """Reject unsafe managed workflow targets before a setup mutates files."""
    root = Path(repo_root)
    for name in _MANAGED:
        read_managed_text(
            root,
            Path(".github") / "workflows" / name,
            max_bytes=_MAX_WORKFLOW_BYTES,
        )


def preflight_workflow_projection(
    repo_root: Path,
    config: MachinistConfig,
    *,
    installed_version: str,
) -> None:
    """Prove a projection cannot overwrite or remove unowned workflow data."""
    root = Path(repo_root)
    expected = expected_workflows(config, installed_version=installed_version)
    current = _read_current_workflows(root)
    unsafe = _unsafe_workflow_transitions(
        current,
        expected,
        config=config,
        installed_version=installed_version,
    )
    if unsafe:
        raise _unsafe_workflow_error(unsafe)


def sync_workflows(
    repo_root: Path,
    config: MachinistConfig,
    *,
    installed_version: str,
    check: bool,
) -> WorkflowSyncReport:
    """Check or write managed workflows; never touch unrelated workflow files."""
    root = Path(repo_root)
    expected = expected_workflows(config, installed_version=installed_version)
    drift: list[str] = []
    current = _read_current_workflows(root)
    for name in _MANAGED:
        actual = current[name]
        wanted = expected.get(name)
        if wanted is None:
            if actual is not None:
                drift.append(name)
        elif actual != wanted:
            drift.append(name)

    if check:
        if drift:
            raise WorkflowDriftError(
                "managed workflow drift: "
                + ", ".join(sorted(drift))
                + "; run 'machinist sync-workflows'"
            )
        return WorkflowSyncReport()

    written: list[str] = []
    removed: list[str] = []
    unsafe = _unsafe_workflow_transitions(
        current,
        expected,
        config=config,
        installed_version=installed_version,
    )
    if unsafe:
        raise _unsafe_workflow_error(unsafe)

    for name in _MANAGED:
        relative = Path(".github") / "workflows" / name
        wanted = expected.get(name)
        if wanted is None:
            if current[name] is not None:
                remove_managed_file(root, relative)
                removed.append(name)
        elif current[name] != wanted:
            write_managed_text(root, relative, wanted)
            written.append(name)
    return WorkflowSyncReport(tuple(written), tuple(removed))


def _read_current_workflows(repo_root: Path) -> dict[str, str | None]:
    return {
        name: read_managed_text(
            repo_root,
            Path(".github") / "workflows" / name,
            max_bytes=_MAX_WORKFLOW_BYTES,
        )
        for name in _MANAGED
    }


def _legacy_pristine_payloads(
    config: MachinistConfig, *, installed_version: str
) -> dict[str, set[str]]:
    """Recognize exact pre-marker projections for a safe one-time upgrade."""
    github = config.github.model_copy(update={"manage_workflows": True})
    managed = config.model_copy(update={"github": github})
    candidates: dict[str, set[str]] = {name: set() for name in _MANAGED}
    for spec_source in (SpecSource.LOCAL, SpecSource.GITHUB_ACTIONS):
        for spec_install in (SpecInstall.PYPI, SpecInstall.CHECKOUT):
            variant_github = github.model_copy(
                update={
                    "spec_source": spec_source,
                    "spec_install": spec_install,
                }
            )
            variant = managed.model_copy(update={"github": variant_github})
            for name, payload in _render_workflow_payloads(
                variant, installed_version=installed_version
            ).items():
                candidates[name].add(payload)
    return candidates


def _unsafe_workflow_transitions(
    current: dict[str, str | None],
    expected: dict[str, str],
    *,
    config: MachinistConfig,
    installed_version: str,
) -> list[str]:
    legacy = _legacy_pristine_payloads(config, installed_version=installed_version)
    unsafe: list[str] = []
    for name in _MANAGED:
        actual = current[name]
        if actual is None or actual == expected.get(name):
            continue
        if not _is_pristine_managed_workflow(actual) and actual not in legacy[name]:
            unsafe.append(name)
    return unsafe


def _unsafe_workflow_error(names: list[str]) -> WorkflowDriftError:
    return WorkflowDriftError(
        "refusing to replace or remove unrecognized/modified workflow(s): "
        + ", ".join(sorted(names))
        + "; review and remove or restore them explicitly"
    )

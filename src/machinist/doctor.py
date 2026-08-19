"""Read-only diagnostics for an AgentMachinist installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from machinist.config import MachinistConfig
from machinist.harness import get_harness
from machinist.workflows import WorkflowDriftError, sync_workflows

_COMMAND_TIMEOUT_SECONDS = 10


class CheckLevel(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class DoctorCheck:
    level: CheckLevel
    name: str
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.level is not CheckLevel.FAIL for check in self.checks)


def _run_read_only(runner, args: list[str], *, cwd: Path):
    """Run one bounded, read-only probe and turn runner failures into data."""
    try:
        return (
            runner(
                args,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            ),
            None,
        )
    except subprocess.TimeoutExpired:
        return None, f"timed out after {_COMMAND_TIMEOUT_SECONDS} seconds"
    except OSError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - diagnostics report probe failures
        return None, f"probe failed: {exc}"


def _command_failure(result) -> str:
    detail = (getattr(result, "stderr", "") or "").strip() or "command failed"
    return detail.splitlines()[0][:300]


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _harness_for(config, phase: str):
    """Resolve new phase profiles while remaining compatible with 0.3 configs."""
    resolver = getattr(config, "harness_for", None)
    return resolver(phase) if callable(resolver) else config.harness


def _repo_from_origin(origin: str) -> str | None:
    """Derive owner/repo from common GitHub HTTPS and SSH remote forms."""
    value = origin.strip().rstrip("/")
    if value.startswith("git@github.com:"):
        path = value.removeprefix("git@github.com:")
    else:
        parsed = urlparse(value)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git")
    parts = path.split("/")
    return "/".join(parts) if len(parts) == 2 and all(parts) else None


def _workspace_check(root: Path, repo_root: Path) -> DoctorCheck:
    """Validate the workspace location using existing ancestors only."""
    try:
        workspace_root = root.expanduser().resolve()
        repository = repo_root.resolve()
    except OSError as exc:
        return DoctorCheck(
            CheckLevel.FAIL, "workspace", f"cannot resolve workspace root: {exc}"
        )

    if workspace_root == Path(workspace_root.anchor):
        return DoctorCheck(
            CheckLevel.FAIL, "workspace", "workspace root cannot be a filesystem root"
        )
    if workspace_root == repository or workspace_root in repository.parents:
        return DoctorCheck(
            CheckLevel.FAIL,
            "workspace",
            "workspace root cannot be the repository or one of its parents",
        )
    if repository in workspace_root.parents:
        return DoctorCheck(
            CheckLevel.WARN,
            "workspace",
            "workspace root is inside the repository; use a separate directory to avoid Git pollution",
        )

    existing = workspace_root
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir():
        return DoctorCheck(
            CheckLevel.FAIL,
            "workspace",
            f"nearest existing parent is not a directory: {existing}",
        )
    if not os.access(existing, os.W_OK | os.X_OK):
        return DoctorCheck(
            CheckLevel.FAIL,
            "workspace",
            f"nearest existing parent is not writable: {existing}",
        )
    return DoctorCheck(
        CheckLevel.PASS,
        "workspace",
        f"{workspace_root} is safely contained; existing parent {existing} is writable",
    )


def _add_repository_checks(checks, root, config, locations, runner):
    repository_ok = False
    if locations["git"]:
        result, error = _run_read_only(
            runner, ["git", "rev-parse", "--show-toplevel"], cwd=root
        )
        if error:
            checks.append(DoctorCheck(CheckLevel.FAIL, "repository", error))
        elif result.returncode != 0:
            checks.append(
                DoctorCheck(CheckLevel.FAIL, "repository", _command_failure(result))
            )
        else:
            output = (result.stdout or "").strip()
            try:
                actual_root = Path(output).expanduser().resolve() if output else None
            except OSError:
                actual_root = None
            if actual_root != root:
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL,
                        "repository",
                        f"expected Git toplevel {root}, got {output or 'no path'}",
                    )
                )
            else:
                repository_ok = True
                checks.append(
                    DoctorCheck(CheckLevel.PASS, "repository", str(actual_root))
                )
    else:
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "repository", "cannot check without git")
        )

    derived_repo = None
    if locations["git"] and repository_ok:
        result, error = _run_read_only(
            runner, ["git", "remote", "get-url", "origin"], cwd=root
        )
        if error:
            checks.append(DoctorCheck(CheckLevel.FAIL, "origin", error))
        elif result.returncode != 0 or not (result.stdout or "").strip():
            checks.append(
                DoctorCheck(CheckLevel.FAIL, "origin", _command_failure(result))
            )
        else:
            origin_url = (result.stdout or "").strip()
            derived_repo = _repo_from_origin(origin_url)
            checks.append(DoctorCheck(CheckLevel.PASS, "origin", origin_url))
    else:
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL, "origin", "cannot check without a Git repository"
            )
        )

    configured_repo = getattr(config.github, "repo", None)
    if configured_repo and derived_repo:
        if configured_repo.casefold() == derived_repo.casefold():
            checks.append(
                DoctorCheck(CheckLevel.PASS, "repository identity", configured_repo)
            )
        else:
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL,
                    "repository identity",
                    f"github.repo is {configured_repo}, but origin resolves to {derived_repo}",
                )
            )
    elif configured_repo:
        checks.append(
            DoctorCheck(
                CheckLevel.WARN,
                "repository identity",
                f"github.repo is {configured_repo}; origin is not a recognizable GitHub URL",
            )
        )
    elif derived_repo:
        checks.append(
            DoctorCheck(
                CheckLevel.PASS,
                "repository identity",
                f"derived {derived_repo} from origin",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                CheckLevel.WARN,
                "repository identity",
                "set github.repo when origin is not a GitHub URL",
            )
        )
    return repository_ok, derived_repo


def _add_github_checks(checks, root, config, locations, runner, derived_repo):
    configured_repo = getattr(config.github, "repo", None)
    if locations["gh"]:
        result, error = _run_read_only(runner, ["gh", "auth", "status"], cwd=root)
        if error:
            checks.append(DoctorCheck(CheckLevel.FAIL, "GitHub authentication", error))
        elif result.returncode == 0:
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS, "GitHub authentication", "gh auth is active"
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL,
                    "GitHub authentication",
                    "gh is not authenticated; run 'gh auth login'",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL, "GitHub authentication", "cannot check without gh"
            )
        )

    repo_args = ["gh", "repo", "view"]
    if configured_repo:
        repo_args += ["--repo", configured_repo]
    repo_args += ["--json", "nameWithOwner,defaultBranchRef"]
    if locations["gh"]:
        result, error = _run_read_only(runner, repo_args, cwd=root)
        if error or result.returncode != 0:
            detail = error or _command_failure(result)
            checks.append(DoctorCheck(CheckLevel.FAIL, "GitHub repository", detail))
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL, "default branch", "repository lookup failed"
                )
            )
        else:
            try:
                data = json.loads(result.stdout or "")
                github_repo = data["nameWithOwner"]
                default_branch = data["defaultBranchRef"]["name"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL,
                        "GitHub repository",
                        f"invalid gh response: {exc}",
                    )
                )
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL, "default branch", "repository lookup failed"
                    )
                )
            else:
                expected_repo = configured_repo or derived_repo
                level = (
                    CheckLevel.FAIL
                    if expected_repo
                    and github_repo.casefold() != expected_repo.casefold()
                    else CheckLevel.PASS
                )
                detail = (
                    f"gh resolved {github_repo}, expected {expected_repo}"
                    if level is CheckLevel.FAIL
                    else github_repo
                )
                checks.append(DoctorCheck(level, "GitHub repository", detail))
                checks.append(
                    DoctorCheck(CheckLevel.PASS, "default branch", default_branch)
                )
    else:
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "GitHub repository", "cannot check without gh")
        )
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "default branch", "cannot check without gh")
        )

    if locations["gh"]:
        label_args = ["gh", "label", "list", "--limit", "1000", "--json", "name"]
        if configured_repo:
            label_args += ["--repo", configured_repo]
        result, error = _run_read_only(runner, label_args, cwd=root)
        if error or result.returncode != 0:
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL, "labels", error or _command_failure(result)
                )
            )
        else:
            try:
                names = {item["name"] for item in json.loads(result.stdout or "")}
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL, "labels", f"invalid gh response: {exc}"
                    )
                )
            else:
                label_config = getattr(config.github, "labels", None)
                required = {
                    getattr(label_config, "trigger", "agent-task"),
                    getattr(label_config, "approved", "machinist:approved"),
                }
                missing = sorted(required - names)
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL,
                        "labels",
                        "missing required labels: " + ", ".join(missing),
                    )
                    if missing
                    else DoctorCheck(
                        CheckLevel.PASS,
                        "labels",
                        "required labels are present and readable",
                    )
                )
    else:
        checks.append(DoctorCheck(CheckLevel.FAIL, "labels", "cannot check without gh"))


def run_doctor(
    repo_root: Path,
    config: MachinistConfig,
    *,
    installed_version: str,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
) -> DoctorReport:
    """Accumulate diagnostics without creating or changing repository files."""
    root = Path(repo_root).expanduser().resolve()
    checks: list[DoctorCheck] = []
    locations: dict[str, str | None] = {}
    for executable in ("git", "gh"):
        try:
            location = which(executable)
        except Exception as exc:  # noqa: BLE001 - report PATH probe failures
            location = None
            checks.append(
                DoctorCheck(CheckLevel.FAIL, executable, f"PATH lookup failed: {exc}")
            )
        else:
            checks.append(
                DoctorCheck(CheckLevel.PASS, executable, location)
                if location
                else DoctorCheck(
                    CheckLevel.FAIL, executable, f"{executable} is not on PATH"
                )
            )
        locations[executable] = location

    repository_ok, derived_repo = _add_repository_checks(
        checks, root, config, locations, runner
    )
    _add_github_checks(checks, root, config, locations, runner, derived_repo)

    try:
        workspace_root = config.workspace.resolved_root()
    except Exception as exc:  # noqa: BLE001 - report invalid third-party config
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "workspace", f"invalid workspace root: {exc}")
        )
    else:
        checks.append(_workspace_check(workspace_root, root))

    if locations["git"] and repository_ok:
        result, error = _run_read_only(
            runner, ["git", "check-ignore", "-q", "--", ".machinist/runs/"], cwd=root
        )
        if error:
            checks.append(DoctorCheck(CheckLevel.FAIL, "runtime state", error))
        elif result.returncode == 0:
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS, "runtime state", ".machinist/runs/ is git-ignored"
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL,
                    "runtime state",
                    ".machinist/runs/ is not git-ignored; runtime output may contain sensitive data",
                )
            )
    else:
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "runtime state", "cannot check without git")
        )

    try:
        execute_config = _harness_for(config, "execute")
        spec_config = _harness_for(config, "spec")
        execute_harness = get_harness(execute_config)
        spec_harness = get_harness(spec_config)
    except Exception as exc:  # noqa: BLE001 - report adapter/config failures
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "harness", f"cannot resolve harness: {exc}")
        )
        spec_config = config.harness
    else:
        harnesses = [("harness", execute_harness)]
        if (spec_harness.command, spec_harness.name) != (
            execute_harness.command,
            execute_harness.name,
        ):
            harnesses.append(("Spec harness", spec_harness))
        for check_name, harness in harnesses:
            try:
                harness_location = (
                    harness.command
                    if Path(harness.command).is_absolute()
                    and Path(harness.command).exists()
                    else which(harness.command)
                )
            except Exception as exc:  # noqa: BLE001 - report PATH probe failures
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL,
                        check_name,
                        f"cannot locate '{harness.command}': {exc}",
                    )
                )
                continue
            checks.append(
                DoctorCheck(CheckLevel.PASS, check_name, str(harness_location))
                if harness_location
                else DoctorCheck(
                    CheckLevel.FAIL,
                    check_name,
                    f"'{harness.command}' is not on PATH; install it or set harness.command",
                )
            )

    spec_source = _enum_value(getattr(config.github, "spec_source", "local"))
    harness_name = _enum_value(getattr(spec_config, "name", "claude-code"))
    manage_workflows = getattr(config.github, "manage_workflows", True)
    if (
        spec_source == "github-actions"
        and harness_name != "claude-code"
        and manage_workflows
    ):
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL,
                "Spec source",
                "github-actions Spec generation currently installs only claude-code; "
                f"configured harness is {harness_name}",
            )
        )
    elif spec_source == "github-actions" and harness_name != "claude-code":
        checks.append(
            DoctorCheck(
                CheckLevel.PASS,
                "Spec source",
                f"{harness_name} is supported by the externally managed Spec workflow",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                CheckLevel.PASS,
                "Spec source",
                f"{spec_source} is compatible with {harness_name}",
            )
        )

    gate_resolver = getattr(config, "resolved_verification_gates", None)
    try:
        gates = tuple(gate_resolver()) if callable(gate_resolver) else ()
    except Exception as exc:  # noqa: BLE001 - report invalid third-party config
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL, "test gate", f"cannot resolve verification: {exc}"
            )
        )
    else:
        legacy_command = getattr(getattr(config, "tests", None), "command", None)
        if gates:
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS,
                    "test gate",
                    "configured verification gates: "
                    + ", ".join(getattr(gate, "name", "unnamed") for gate in gates),
                )
            )
        elif legacy_command:
            checks.append(DoctorCheck(CheckLevel.PASS, "test gate", legacy_command))
        else:
            checks.append(
                DoctorCheck(
                    CheckLevel.WARN,
                    "test gate",
                    "no verification gates are configured; implementations can be marked ready without tests",
                )
            )

    if not manage_workflows:
        checks.append(
            DoctorCheck(
                CheckLevel.PASS,
                "workflows",
                "managed workflows are disabled; drift check skipped",
            )
        )
    else:
        try:
            sync_workflows(
                root, config, installed_version=installed_version, check=True
            )
        except WorkflowDriftError as exc:
            checks.append(DoctorCheck(CheckLevel.FAIL, "workflows", str(exc)))
        except Exception as exc:  # noqa: BLE001 - doctor must aggregate failures
            checks.append(
                DoctorCheck(CheckLevel.FAIL, "workflows", f"drift check failed: {exc}")
            )
        else:
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS, "workflows", "managed workflows match config"
                )
            )

    try:
        run_files = sorted((root / ".machinist/runs").glob("issue-*-*.json"))
        needs_attention: list[str] = []
        malformed: list[str] = []
        for path in run_files:
            try:
                status = json.loads(path.read_text()).get("status")
            except (OSError, json.JSONDecodeError):
                malformed.append(path.name)
                continue
            if status in {"running", "failed"}:
                needs_attention.append(path.name)
    except OSError as exc:
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL, "Task Runs", f"cannot inspect runtime state: {exc}"
            )
        )
    else:
        if malformed:
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL,
                    "Task Runs",
                    "unreadable runtime state: " + ", ".join(malformed),
                )
            )
        elif needs_attention:
            checks.append(
                DoctorCheck(
                    CheckLevel.WARN,
                    "Task Runs",
                    f"{len(needs_attention)} failed or abandoned run(s); inspect, then use 'machinist retry'",
                )
            )
        else:
            checks.append(
                DoctorCheck(CheckLevel.PASS, "Task Runs", "no failed or abandoned runs")
            )

    return DoctorReport(tuple(checks))

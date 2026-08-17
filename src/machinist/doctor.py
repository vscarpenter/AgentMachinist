"""Read-only diagnostics for an AgentMachinist installation."""

from __future__ import annotations

import shutil
import subprocess
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from machinist.config import MachinistConfig
from machinist.harness import get_harness
from machinist.workflows import WorkflowDriftError, sync_workflows


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


def run_doctor(
    repo_root: Path,
    config: MachinistConfig,
    *,
    installed_version: str,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
) -> DoctorReport:
    """Accumulate diagnostics without creating or changing repository files."""
    root = Path(repo_root)
    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(CheckLevel.PASS, "repository", str(root))
        if (root / ".git").exists()
        else DoctorCheck(CheckLevel.FAIL, "repository", "current directory is not a Git checkout")
    )

    for executable in ("git", "gh"):
        location = which(executable)
        checks.append(
            DoctorCheck(CheckLevel.PASS, executable, location)
            if location
            else DoctorCheck(CheckLevel.FAIL, executable, f"{executable} is not on PATH")
        )

    if which("gh"):
        result = runner(
            ["gh", "auth", "status"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        checks.append(
            DoctorCheck(CheckLevel.PASS, "GitHub authentication", "gh auth is active")
            if result.returncode == 0
            else DoctorCheck(
                CheckLevel.FAIL,
                "GitHub authentication",
                "gh is not authenticated; run 'gh auth login'",
            )
        )
    else:
        checks.append(
            DoctorCheck(CheckLevel.FAIL, "GitHub authentication", "cannot check without gh")
        )

    harness = get_harness(config.harness)
    harness_location = (
        harness.command
        if Path(harness.command).is_absolute() and Path(harness.command).exists()
        else which(harness.command)
    )
    checks.append(
        DoctorCheck(CheckLevel.PASS, "harness", str(harness_location))
        if harness_location
        else DoctorCheck(
            CheckLevel.FAIL,
            "harness",
            f"'{harness.command}' is not on PATH; install it or set harness.command",
        )
    )

    checks.append(
        DoctorCheck(CheckLevel.PASS, "test gate", config.tests.command)
        if config.tests.command
        else DoctorCheck(
            CheckLevel.WARN,
            "test gate",
            "tests.command is null; implementations can be marked ready without tests",
        )
    )

    try:
        sync_workflows(
            root,
            config,
            installed_version=installed_version,
            check=True,
        )
    except WorkflowDriftError as exc:
        checks.append(DoctorCheck(CheckLevel.FAIL, "workflows", str(exc)))
    else:
        checks.append(DoctorCheck(CheckLevel.PASS, "workflows", "managed workflows match config"))

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
        checks.append(DoctorCheck(CheckLevel.PASS, "Task Runs", "no failed or abandoned runs"))

    return DoctorReport(tuple(checks))

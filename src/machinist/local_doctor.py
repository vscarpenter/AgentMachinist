"""Optional local readiness using start's configuration and shared diagnostics."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from machinist.config import (
    MachinistConfig,
    VerificationGateConfig,
    WorkspaceConfig,
    WorkspaceStrategy,
)
from machinist.doctor import (
    _COMMAND_TIMEOUT_SECONDS,
    CheckLevel,
    DoctorCheck,
    DoctorReport,
    _add_harness_checks,
    _command_failure,
    _run_gate_readiness,
    _run_read_only,
    _verification_command_check,
    _workspace_writability_check,
    fix_hint_for_check_name,
)
from machinist.local_setup import resolve_local_config
from machinist.local_workspace import LocalWorkspace
from machinist.process import run_supervised
from machinist.verification import (
    VerificationError,
    VerificationFailed,
    run_verification_gates,
)
from machinist.workspace import Workspace, WorkspaceError

_FIX_HINTS = {
    "repository": "run inside a Git repository, or initialize one with git init",
    "committed HEAD": "review and commit the initial project files with git add and git commit",
    "local branch": "switch to a named local branch with git switch <branch>",
    "Git author": "configure your identity with git config user.name and git config user.email",
    "working tree": "commit or stash pending changes before starting a local Task",
    "local configuration": (
        "correct the reported configuration; before first setup, use "
        "machinist start <task> --harness <name> --test-cmd '<command>'"
    ),
    "harness": (
        "install and authenticate the configured Harness; correct saved choices in "
        ".machinist/runs/local/config.yaml, or choose --harness on the first start"
    ),
    "test gate": (
        "configure a required verification gate in the local configuration, or "
        "pass --test-cmd '<command>' on the first start"
    ),
    "verification commands": (
        "make the reported gate entry point executable and available, or correct "
        "the gate in .machinist/runs/local/config.yaml"
    ),
    "verification execution": (
        "resolve failed readiness checks and gates, then rerun machinist doctor --local --run-gates"
    ),
    "fresh Workshop": (
        "make configured gates prepare dependencies from committed files, then rerun "
        "machinist doctor --local --fresh-workshop; ignored dependencies in the "
        "controller checkout are not copied"
    ),
    "workspace": (
        "choose a writable workspace.root outside this repository in the local configuration"
    ),
    "runtime exclusion": (
        "correct the reported Git exclude path, or add /.machinist/runs/ to "
        ".gitignore and resolve any overriding repository ignore rules"
    ),
}


def local_fix_hint_for_check_name(
    name: str, *, fresh_workshop: bool = False
) -> str | None:
    """Keep local remediation independent of GitHub setup and managed workflows."""
    if fresh_workshop and name == "verification execution":
        return (
            "make dependencies available from committed files and the configured "
            "check command, then rerun machinist doctor --local --fresh-workshop"
        )
    return _FIX_HINTS.get(name) or fix_hint_for_check_name(name)


def _git_probe(workspace: Workspace, root: Path, *args: str):
    # Keep Git's controller environment and disabled hooks/fsmonitor, while
    # preventing status from taking an optional lock and refreshing the index.
    return _run_read_only(
        lambda arguments, **kwargs: workspace._run(root, *arguments),
        ["--no-optional-locks", *args],
        cwd=root,
    )


def _author_check(workspace: Workspace, root: Path) -> DoctorCheck:
    email, email_error = _git_probe(workspace, root, "config", "user.email")
    if email_error or email.returncode not in (0, 1):
        return DoctorCheck(
            CheckLevel.FAIL, "Git author", email_error or _command_failure(email)
        )
    if email.returncode == 1 or not email.stdout.strip():
        # Workspace.commit_all intentionally substitutes both identity fields
        # when repository user.email is absent. Diagnosis must not make this
        # supported path require another onboarding step.
        return DoctorCheck(
            CheckLevel.WARN,
            "Git author",
            "no repository user.email; commits use the existing AgentMachinist "
            "identity fallback (global Git identity is not used by the controller)",
        )
    errors = []
    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        result, error = _git_probe(workspace, root, "var", identity)
        if error or result.returncode != 0 or not result.stdout.strip():
            errors.append(
                error or "configured repository identity is incomplete or invalid"
            )
    return DoctorCheck(
        CheckLevel.FAIL if errors else CheckLevel.PASS,
        "Git author",
        "; ".join(dict.fromkeys(errors))
        if errors
        else "Git can resolve the repository author and committer identity",
    )


def _runtime_exclusion_check(root: Path, config: MachinistConfig) -> DoctorCheck:
    try:
        pending = LocalWorkspace(
            root, config.workspace
        ).runtime_exclusion_needs_update()
    except (WorkspaceError, OSError, ValueError) as exc:
        return DoctorCheck(CheckLevel.FAIL, "runtime exclusion", str(exc))
    return DoctorCheck(
        CheckLevel.WARN if pending else CheckLevel.PASS,
        "runtime exclusion",
        "runtime exclusion is not established; start will apply and verify it; "
        "repository ignore rules may override it"
        if pending
        else "local runtime records are already excluded by Git",
    )


def _add_local_git_checks(checks, workspace: Workspace, root: Path) -> None:
    probes = (
        (
            "committed HEAD",
            ("rev-parse", "--verify", "HEAD^{commit}"),
            "a committed base is available",
            "no committed HEAD; create the initial commit before starting a Task",
        ),
        (
            "local branch",
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
            None,
            "HEAD is detached; switch to a named local base branch",
        ),
    )
    for name, args, success, failure in probes:
        result, error = _git_probe(workspace, root, *args)
        okay = error is None and result.returncode == 0 and bool(result.stdout.strip())
        checks.append(
            DoctorCheck(
                CheckLevel.PASS if okay else CheckLevel.FAIL,
                name,
                (success or result.stdout.strip()) if okay else error or failure,
            )
        )

    checks.append(_author_check(workspace, root))

    tracked, tracked_error = _git_probe(workspace, root, "ls-files", ".machinist/runs/")
    result, error = _git_probe(
        workspace,
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "--",
        ".",
        ":(exclude).machinist/runs",
    )
    if tracked_error or tracked.returncode != 0 or error or result.returncode != 0:
        detail = (
            tracked_error
            or error
            or _command_failure(tracked if tracked.returncode != 0 else result)
        )
    elif tracked.stdout.strip():
        detail = "controller runtime files must not be tracked by Git"
    elif result.stdout.strip():
        detail = "pending tracked or untracked changes; start needs a clean committed checkout"
    else:
        checks.append(DoctorCheck(CheckLevel.PASS, "working tree", "checkout is clean"))
        return
    checks.append(DoctorCheck(CheckLevel.FAIL, "working tree", detail))


def _run_fresh_workshop_readiness(
    root: Path,
    config: MachinistConfig,
    gates: tuple[VerificationGateConfig, ...],
    *,
    gate_runner,
) -> tuple[DoctorCheck, ...]:
    """Prove start's committed baseline without creating a Task or local state.

    A disposable clone uses production provisioning/custody but leaves the
    controller's Git worktree registrations, refs and configuration untouched.
    Its enclosing temporary directory owns cleanup even if a Gate violates
    custody and normal Workshop cleanup would correctly refuse to proceed.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="machinist-doctor-fresh-") as folder:
            temporary = Path(folder)
            settings = config.workspace.model_copy(
                update={
                    "root": temporary / "workshops",
                    "strategy": WorkspaceStrategy.CLONE,
                }
            )
            workspace = LocalWorkspace(root, settings)
            base = workspace.resolve_commit()
            controller_changes = workspace.change_snapshot(root)
            path = workspace.provision(
                "doctor-readiness", f"{settings.branch_prefix}doctor-readiness", base
            )
            before = workspace.capture_harness_state(path)
            selected_runner = (
                run_supervised if gate_runner is subprocess.run else gate_runner
            )

            def guarded_runner(*args, **kwargs):
                checkpoint = workspace.capture_harness_state(path)
                try:
                    return selected_runner(*args, **kwargs)
                finally:
                    workspace.assert_harness_state(path, checkpoint)
                    if workspace.change_snapshot(root) != controller_changes:
                        raise WorkspaceError(
                            "Verification changed the controller working tree"
                        )

            try:
                report = run_verification_gates(
                    path,
                    gates,
                    log_dir=temporary / "verification-logs",
                    snapshotter=workspace.change_snapshot,
                    runner=guarded_runner,
                )
            except VerificationFailed as exc:
                verification = DoctorCheck(
                    CheckLevel.FAIL,
                    "verification execution",
                    f"fresh Workshop {base}: {exc}",
                )
            else:
                workspace.assert_harness_state(path, before)
                changed = workspace.changed_files(path)
                if changed:
                    raise WorkspaceError(
                        "fresh baseline Verification changed the Workshop "
                        f"({', '.join(changed[:5])}); commit generated files or ignore them"
                    )
                if report.advisory_failures:
                    failures = ", ".join(gate.name for gate in report.advisory_failures)
                    verification = DoctorCheck(
                        CheckLevel.WARN,
                        "verification execution",
                        f"fresh Workshop {base}: required gates passed; advisory failures: {failures}",
                    )
                else:
                    verification = DoctorCheck(
                        CheckLevel.PASS,
                        "verification execution",
                        f"fresh Workshop {base}: all {len(report.gates)} configured gate(s) passed",
                    )
            return (
                DoctorCheck(
                    CheckLevel.PASS,
                    "fresh Workshop",
                    f"checked committed HEAD {base} in a disposable local clone; no Task recorded",
                ),
                verification,
            )
    except (WorkspaceError, VerificationError, OSError, ValueError) as exc:
        return (
            DoctorCheck(CheckLevel.FAIL, "fresh Workshop", str(exc)),
            DoctorCheck(
                CheckLevel.FAIL,
                "verification execution",
                f"fresh Workshop readiness could not complete safely: {exc}",
            ),
        )


def run_local_doctor(
    repo_root: Path,
    *,
    run_gates: bool = False,
    fresh_workshop: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    runner=subprocess.run,
    gate_runner=run_supervised,
) -> DoctorReport:
    """Diagnose local start without adopting the repository or invoking a model.

    Gate execution is explicitly opt-in and can run project commands. The
    fresh Workshop option implies execution in a disposable committed checkout;
    otherwise run_gates keeps using the controller checkout. Logs and fresh
    checkouts use temporary directories, never local Task runtime state.
    """
    root = Path(repo_root).expanduser().resolve()
    checks: list[DoctorCheck] = []
    try:
        location = which("git")
    except Exception as exc:  # noqa: BLE001 - diagnostics aggregate PATH failures
        checks.append(DoctorCheck(CheckLevel.FAIL, "git", f"PATH lookup failed: {exc}"))
        return DoctorReport(tuple(checks))
    if not location:
        return DoctorReport(
            (DoctorCheck(CheckLevel.FAIL, "git", "git is not on PATH"),)
        )
    checks.append(DoctorCheck(CheckLevel.PASS, "git", location))

    def bounded_runner(args, **kwargs):
        kwargs["timeout"] = _COMMAND_TIMEOUT_SECONDS
        try:
            return runner(args, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise OSError(
                f"Git readiness probe timed out after {_COMMAND_TIMEOUT_SECONDS} seconds"
            ) from exc

    workspace = Workspace(root, WorkspaceConfig(), runner=bounded_runner)
    result, error = _git_probe(workspace, root, "rev-parse", "--show-toplevel")
    if error or result.returncode != 0 or not result.stdout.strip():
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL,
                "repository",
                error
                or "current directory is not a Git working tree; run git init or change directory",
            )
        )
        return DoctorReport(tuple(checks))
    root = Path(result.stdout.strip()).resolve()
    checks.append(DoctorCheck(CheckLevel.PASS, "repository", str(root)))
    _add_local_git_checks(checks, workspace, root)

    try:
        config = resolve_local_config(root, which=which)
    except Exception as exc:  # noqa: BLE001 - isolate configuration and plugin failures
        checks.append(DoctorCheck(CheckLevel.FAIL, "local configuration", str(exc)))
        if run_gates or fresh_workshop:
            if fresh_workshop:
                checks.append(
                    DoctorCheck(
                        CheckLevel.FAIL,
                        "fresh Workshop",
                        "skipped: local configuration is not ready",
                    )
                )
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL,
                    "verification execution",
                    "skipped: local configuration is not ready",
                )
            )
        return DoctorReport(tuple(checks))

    saved = (root / ".machinist/runs/local/config.yaml").is_file()
    checks.append(
        DoctorCheck(
            CheckLevel.PASS,
            "local configuration",
            "using saved .machinist/runs/local/config.yaml"
            if saved
            else "first-start settings resolved from project configuration and discovery; not saved",
        )
    )
    checks.append(_workspace_writability_check(config.workspace.resolved_root()))
    checks.append(_runtime_exclusion_check(root, config))

    def local_which(command):
        return which(str(root / command) if "/" in command else command)

    _add_harness_checks(checks, root, config, local_which, runner)
    gates = config.resolved_verification_gates()
    required = [gate.command for gate in gates if gate.required]
    noun = "gate" if len(required) == 1 else "gates"
    checks.append(
        DoctorCheck(
            CheckLevel.PASS,
            "test gate",
            f"{len(required)} required {noun}: {'; '.join(required)}",
        )
    )
    checks.append(_verification_command_check(gates, root, which))
    if not run_gates and not fresh_workshop:
        checks.append(
            DoctorCheck(
                CheckLevel.WARN,
                "verification execution",
                "not run; use machinist doctor --local --run-gates to execute configured gates",
            )
        )
    elif any(check.level is CheckLevel.FAIL for check in checks):
        if fresh_workshop:
            checks.append(
                DoctorCheck(
                    CheckLevel.FAIL,
                    "fresh Workshop",
                    "skipped: resolve failed readiness checks before provisioning",
                )
            )
        checks.append(
            DoctorCheck(
                CheckLevel.FAIL,
                "verification execution",
                "skipped: resolve failed readiness checks before running project commands",
            )
        )
    elif fresh_workshop:
        checks.extend(
            _run_fresh_workshop_readiness(
                root, config, tuple(gates), gate_runner=gate_runner
            )
        )
    else:
        checks.append(_run_gate_readiness(root, config, gates, gate_runner=gate_runner))
    return DoctorReport(tuple(checks))

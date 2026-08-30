"""Interactive first-run questions for `machinist init`.

Each question prints a one-line explanation before prompting, and every
prompt shows a safe default so Enter-through produces a working config.
Flags passed to `machinist init` pre-answer questions, which the wizard
then skips.
"""

from dataclasses import dataclass

import click

from machinist.config import HarnessName

_LANGUAGE_TEST_COMMANDS = {
    "python": "pytest",
    "javascript": "npm test",
    "rust": "cargo test",
    "go": "go test ./...",
    "java": "mvn test",
}

# Preset -> (backend replacement, events replacement); None keeps the
# template default (desktop backend, [failure] events).
_NOTIFICATION_PRESETS = {
    "failures": (None, None),
    "all": (None, ["failure", "spec_ready", "approval_stale", "pr_ready"]),
    "none": ("disabled", None),
}


@dataclass(frozen=True)
class InitAnswers:
    spec_source: str | None
    harness_name: str | None
    test_command: str | None
    install_workflows: bool
    notification_backend: str | None
    notification_events: list[str] | None


def run_init_wizard(
    *,
    detected_test_command: str | None,
    spec_source: str | None,
    harness_name: str | None,
    test_command: str | None,
    install_workflows: bool | None,
    notifications: str | None,
) -> InitAnswers:
    """Collect init answers, prompting only for values no flag provided."""
    click.echo(
        "Configuring machinist.yaml — press Enter to accept a [default], "
        "or rerun with --no-input to skip these questions."
    )
    resolved_spec_source = spec_source or _ask_spec_source()
    resolved_workflows = (
        install_workflows
        if install_workflows is not None
        else _ask_install_workflows(resolved_spec_source)
    )
    resolved_harness = harness_name or _ask_harness(
        spec_source=resolved_spec_source, manage_workflows=resolved_workflows
    )
    resolved_test_command = (
        test_command
        if test_command is not None
        else _ask_test_command(detected_test_command)
    )
    if notifications is not None:
        backend, events = notifications, None
    else:
        backend, events = _NOTIFICATION_PRESETS[_ask_notifications()]
    return InitAnswers(
        spec_source=resolved_spec_source,
        harness_name=resolved_harness,
        test_command=resolved_test_command,
        install_workflows=resolved_workflows,
        notification_backend=backend,
        notification_events=events,
    )


def _ask_spec_source() -> str:
    click.echo(
        "\nDispatch mode — who runs the Spec phase when you label an issue:\n"
        "  local           this machine runs it via 'machinist watch'\n"
        "  github-actions  GitHub CI runs it (requires the selected Harness'"
        " API-key repository secret)"
    )
    return click.prompt(
        "Spec dispatch",
        type=click.Choice(["local", "github-actions"]),
        default="local",
        show_choices=False,
    )


def _ask_install_workflows(spec_source: str) -> bool:
    click.echo(
        "\nManaged workflows — .github/workflows files Machinist writes and keeps\n"
        "in sync so approval marking (and CI dispatch) work out of the box."
    )
    if spec_source == "github-actions":
        click.echo("github-actions dispatch relies on the managed spec workflow.")
    return click.confirm("Install managed GitHub workflows?", default=True)


def _ask_harness(*, spec_source: str, manage_workflows: bool) -> str:
    click.echo(
        "\nHarness — the coding agent CLI that writes your specs and implementation."
    )
    return click.prompt(
        "Harness",
        type=click.Choice([harness.value for harness in HarnessName]),
        default=HarnessName.CLAUDE_CODE.value,
    )


def _ask_test_command(detected: str | None) -> str | None:
    click.echo(
        "\nTest gate — command that must pass before an implementation PR is\n"
        "marked ready for review."
    )
    if detected and click.confirm(
        f"Use detected test command '{detected}'?", default=True
    ):
        return detected
    language = click.prompt(
        "Language for a suggested command",
        type=click.Choice([*_LANGUAGE_TEST_COMMANDS, "other", "skip"]),
        default="skip",
    )
    if language == "skip":
        click.echo("Skipping the test gate; set tests.command in machinist.yaml later.")
        return None
    suggestion = _LANGUAGE_TEST_COMMANDS.get(language)
    if suggestion is None:
        return click.prompt("Test gate command")
    return click.prompt("Test gate command", default=suggestion)


def _ask_notifications() -> str:
    click.echo(
        "\nNotifications — desktop alerts while the pipeline runs in the"
        " background:\n"
        "  failures  notify only when a run fails\n"
        "  all       failures plus spec-ready, stale-approval, and PR-ready"
        " events\n"
        "  none      disable notifications"
    )
    return click.prompt(
        "Notify on",
        type=click.Choice(["failures", "all", "none"]),
        default="failures",
    )

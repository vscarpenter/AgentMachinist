"""Drift tests: docs/getting-started.md must stay true to the code.

Guide conventions these tests rely on:
- Every ```yaml fenced block is a machinist.yaml snippet rooted at the
  top level, so it validates against MachinistConfig (extra="forbid").
- Prose never follows the word 'machinist' with a bare lowercase word
  unless it is a real subcommand (write 'AgentMachinist' or
  '.machinist/' instead).
"""

import re
import tomllib
from pathlib import Path

import click
import pytest
import yaml

from machinist.cli import main
from machinist.config import LabelsConfig, MachinistConfig
from machinist.phases.status import PIPELINE_STATES

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GUIDE_PATH = _REPO_ROOT / "docs" / "getting-started.md"
_FIRST_RUN_GUIDE_PATH = _REPO_ROOT / "docs" / "first-run-guide.html"
_ONBOARDING_PATH = _REPO_ROOT / "docs" / "onboarding.html"
_HARNESS_PATH = _REPO_ROOT / "docs" / "harnesses.md"
_EXPLAINER_PATH = _REPO_ROOT / "docs" / "explainer.html"
_README_PATH = _REPO_ROOT / "README.md"
_CLAUDE_PATH = _REPO_ROOT / "CLAUDE.md"
_CHANGELOG_PATH = _REPO_ROOT / "CHANGELOG.md"

if not (_REPO_ROOT / "docs").exists():
    pytest.skip(
        "repository-only test (paths absent from sdist)", allow_module_level=True
    )

_REQUIRED_HEADINGS = (
    "# Getting Started with AgentMachinist",
    "## What is AgentMachinist?",
    "## Before you begin",
    "## Install",
    "## Set up your repository",
    "## Your first agent task",
    "## Review and approve",
    "## Spec generation: local or CI",
    "## Configuration reference",
    "## Choosing a harness",
    "## Troubleshooting",
    "## Operational limits",
)

_REQUIRED_DOCS = (
    "architecture.md",
    "operator-runbook.md",
    "trust-model.md",
    "harnesses.md",
)


def _guide_text() -> str:
    assert _GUIDE_PATH.is_file(), "docs/getting-started.md is missing"
    return _GUIDE_PATH.read_text()


def test_guide_exists_with_required_sections():
    lines = _guide_text().splitlines()
    positions = []
    for heading in _REQUIRED_HEADINGS:
        assert heading in lines, f"guide is missing the heading: {heading}"
        positions.append(lines.index(heading))
    assert positions == sorted(positions), "guide sections are out of order"


def test_readme_links_to_guide():
    assert (
        "https://github.com/vscarpenter/AgentMachinist/blob/main/docs/getting-started.md"
        in _README_PATH.read_text()
    )


def test_guide_subcommands_are_real():
    # \b keeps prose like 'AgentMachinist writes ...' from matching.
    mentioned = set(re.findall(r"\bmachinist ([a-z][a-z-]*)", _guide_text()))
    assert mentioned, "guide never shows a machinist subcommand"
    unknown = mentioned - set(main.commands)
    assert not unknown, f"guide mentions unregistered subcommands: {sorted(unknown)}"


def test_guide_flags_are_real():
    real_flags = {"--help"}
    commands: list[click.Command] = [main]
    visited: set[int] = set()
    while commands:
        command = commands.pop()
        if id(command) in visited:
            continue
        visited.add(id(command))
        for param in command.params:
            real_flags.update(
                opt
                for opt in (*param.opts, *param.secondary_opts)
                if opt.startswith("--")
            )
        if isinstance(command, click.Group):
            commands.extend(command.commands.values())
    shown = set()
    for invocation in re.findall(r"\bmachinist [^\n`]*", _guide_text()):
        shown.update(re.findall(r"--[a-z][a-z-]*", invocation))
    assert shown, "guide never shows a machinist flag"
    unknown = shown - real_flags
    assert not unknown, f"guide shows flags that do not exist: {sorted(unknown)}"


def test_guide_yaml_blocks_validate():
    blocks = re.findall(r"```yaml\n(.*?)```", _guide_text(), flags=re.DOTALL)
    assert blocks, "guide has no yaml config examples"
    for block in blocks:
        MachinistConfig.model_validate(yaml.safe_load(block) or {})


def test_guide_uses_real_label_names():
    text = _guide_text()
    labels = LabelsConfig()
    assert labels.trigger in text, f"guide never mentions the '{labels.trigger}' label"
    assert labels.approved in text, (
        f"guide never mentions the '{labels.approved}' label"
    )


def test_operator_trust_architecture_and_harness_docs_exist():
    for name in _REQUIRED_DOCS:
        assert (_REPO_ROOT / "docs" / name).is_file(), f"docs/{name} is missing"


def test_documented_pipeline_states_match_implementation_constants():
    combined = _guide_text() + (_REPO_ROOT / "docs/operator-runbook.md").read_text()
    for state in PIPELINE_STATES:
        assert f"`{state}`" in combined, f"state '{state}' is undocumented"


def test_stale_milestone_and_approval_claims_cannot_return():
    current_docs = "\n".join(
        path.read_text()
        for path in (
            _README_PATH,
            _GUIDE_PATH,
            *(_REPO_ROOT / "docs" / name for name in _REQUIRED_DOCS),
        )
    ).lower()
    forbidden = (
        "nothing consumes it yet",
        "coming watch daemon",
        "package is not on pypi",
        "label is the durable approval record",
        "agent has no git access",
    )
    for phrase in forbidden:
        assert phrase not in current_docs


def test_superseded_handbook_redirects_to_canonical_guide():
    html = _ONBOARDING_PATH.read_text().lower()
    for required in (
        "<!doctype html>",
        '<html lang="en">',
        '<meta name="viewport"',
        '<meta name="robots" content="noindex">',
        'content="0; url=first-run-guide.html"',
        'data-document-status="archived"',
        'href="#main"',
        "<nav aria-label=",
        '<main id="main"',
        'href="first-run-guide.html"',
        "</html>",
    ):
        assert required in html
    assert "machinist retry" not in html
    assert "docs/onboarding.html" not in _README_PATH.read_text()


def test_first_run_guide_is_visual_interactive_and_linked():
    assert _FIRST_RUN_GUIDE_PATH.is_file(), "docs/first-run-guide.html is missing"
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    for required in (
        "<!doctype html>",
        '<html lang="en">',
        '<meta name="viewport"',
        'href="#main"',
        '<nav class="nav shell" aria-label=',
        '<main id="main">',
        'role="group" aria-label="choose spec generation mode"',
        'data-mode-button="local" aria-pressed="true"',
        'data-mode-button="ci" aria-pressed="false"',
        'aria-live="polite"',
        "</html>",
    ):
        assert required in html
    assert html.count("<svg") >= 8, "first-run guide should remain visually led"
    assert "machinist approve --pr 18" in html
    assert "machinist run 42" in html
    assert "/machinist-execute" in html
    assert "approval stale" in html
    assert "agentmachinist never" in html and "merge" in html
    assert (
        "https://vscarpenter.github.io/AgentMachinist/first-run-guide.html"
        in _README_PATH.read_text()
    ), "README must link the rendered guide, not the raw blob"


def test_setup_docs_require_review_commit_and_push():
    readme = _README_PATH.read_text().lower()
    guide = _guide_text().lower()
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    for text in (readme, guide, html):
        assert ".machinist/runs/" in text
        assert "git add -p .github/workflows" in text
        assert "git diff --cached" in text
        assert "git commit" in text
        assert "git push" in text
        assert (
            "git add machinist.yaml .machinist/specs/.gitkeep "
            ".github/workflows .gitignore"
        ) not in text
    assert "--no-workflows" in guide
    assert "manage_workflows: false" in guide
    assert "drift checking is disabled" in guide
    assert "idempotently adds" in guide and ".gitignore" in guide


def test_spec_lifecycle_and_execute_recovery_commands_are_documented():
    guide = _guide_text().lower()
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    operator = (_REPO_ROOT / "docs" / "operator-runbook.md").read_text().lower()
    architecture = (_REPO_ROOT / "docs" / "architecture.md").read_text().lower()
    combined = "\n".join((guide, html, operator, architecture))
    normalized_guide = " ".join(guide.split())
    assert "doc-pending" not in combined
    assert "data-doc-pending" not in combined
    for command in (
        "machinist spec 42 --revise",
        'machinist spec 42 --abandon --reason "requirements changed"',
        "machinist retry 42 --phase execute --run --resume",
        "machinist retry 42 --phase execute --run --fresh",
    ):
        assert command in html
    assert "fresh is the default" in normalized_guide
    assert "fresh is the default" in operator
    assert "existing branch and draft pr" in normalized_guide


def test_harness_auth_and_security_sensitive_config_are_documented():
    guide = _guide_text().lower()
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    harnesses = _HARNESS_PATH.read_text().lower()
    for text in (guide, html):
        assert "model" in text
        assert "extra_args" in text
        assert "sandbox" in text and "permission" in text
    for command in (
        "claude auth status",
        "claude auth login",
        "codex login status",
        "codex login",
        "opencode auth list",
        "opencode auth login",
        "pi auth check --model <model>",
    ):
        assert f"`{command}`" in harnesses
    assert "<code>claude login</code>" not in html
    assert "<code>pi auth</code>" not in html


def test_readme_lists_recovery_inspection_and_cleanup_commands():
    readme = _README_PATH.read_text()
    assert "`machinist inspect <issue> [--offline] [--json]`" in readme
    assert r"`machinist clean [--issue <issue>\|--all]`" in readme


def test_solo_operator_surfaces_and_advanced_config_are_documented():
    readme = _README_PATH.read_text().lower()
    guide = _guide_text().lower()
    operator = (_REPO_ROOT / "docs" / "operator-runbook.md").read_text().lower()
    architecture = (_REPO_ROOT / "docs" / "architecture.md").read_text().lower()
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    combined = "\n".join((readme, guide, operator, architecture, html))

    for command in (
        "machinist service install",
        "machinist service restart",
        "machinist service uninstall",
        "machinist queue pause",
        "machinist queue defer",
        "machinist cancel 42",
        "machinist amend 42",
        "machinist spec 7 --dry-run",
        "machinist config validate",
        "machinist config show",
        "machinist config schema",
        "machinist config set",
        "machinist status --local --json",
        "machinist status --all --json",
        "machinist runs --issue 42 --json",
        "machinist inspect 42 --offline --json",
        "machinist repo add",
        "machinist watch --dry-run",
    ):
        assert command in combined

    for setting in (
        "verification.gates",
        "mutation_policy",
        "instructions:",
        "harness profiles",
        "notifications:",
        "allowed_hours",
        "task_budget",
        "max_changed_files",
    ):
        assert setting in combined
    assert "runs `machinist watch --once`" in combined
    assert "uninstall" in operator and "preserves logs" in operator


def test_first_run_guide_describes_verification_and_cleanup_precisely():
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    assert "when no named <code>verification.gates</code> exist" in html
    assert "after the successful execute run completed" in html
    assert "removed when the tests passed" not in html
    assert "this is an abridged first-run configuration" in html


def test_release_docs_describe_current_package_version():
    version = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    assert f"agentmachinist {version} is available on pypi" in html
    assert "uv tool install agentmachinist" in html
    assert "unreleased" not in html
    major_minor = ".".join(version.split(".")[:2])
    assert f"a visual field guide for the {major_minor} release" in html
    assert f"agentmachinist {major_minor} first-run field guide" in html
    changelog = _CHANGELOG_PATH.read_text()
    assert f"## {version} —" in changelog
    assert (
        f"https://pypi.org/project/agentmachinist/{version}/"
        in _README_PATH.read_text()
    )
    assert f"current release: {version}" in _CLAUDE_PATH.read_text().lower()
    explainer = _EXPLAINER_PATH.read_text().lower()
    assert f'<span class="hud-badge">v{version}</span>' in explainer
    assert f"install agentmachinist {version} from pypi" in explainer
    release_text = _README_PATH.read_text().lower().split("## releasing", 1)[1]
    assert "sha-256" in release_text
    assert "trusted publishing" in release_text
    assert "exact version" in release_text
    assert release_text.index("smoke-tests") < release_text.index("publishes")
    assert release_text.index("publishes") < release_text.index("attach")

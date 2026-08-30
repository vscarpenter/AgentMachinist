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
_JOB_CARD_PATH = _REPO_ROOT / "docs" / "job-card.html"
_TRUST_MODEL_PATH = _REPO_ROOT / "docs" / "trust-model.md"
_DOCS_INDEX_PATH = _REPO_ROOT / "docs" / "README.md"
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
    "README.md",
    "architecture.md",
    "operator-runbook.md",
    "trust-model.md",
    "harnesses.md",
)

_HISTORICAL_DOCS = (
    _REPO_ROOT / "docs/superpowers/plans/2026-08-17-build-system-hardening.md",
    _REPO_ROOT / "docs/superpowers/specs/2026-08-16-agentmachinist-design.md",
    _REPO_ROOT
    / "docs/superpowers/specs/2026-08-17-reliability-and-usability-hardening.md",
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
        "https://agentmachinist.vinny.dev/first-run-guide.html"
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
        "claude auth status --json",
        "claude auth login",
        "codex login status",
        "codex login",
        "opencode auth list --pure",
        "opencode auth login",
        "pi auth check --model <model> --json --no-refresh",
    ):
        assert f"`{command}`" in harnesses
    assert "<code>claude login</code>" not in html
    assert "<code>pi auth</code>" not in html


def test_adoption_docs_expose_readiness_progress_and_platform_boundaries():
    readme = _README_PATH.read_text().lower()
    guide = _guide_text().lower()
    operator = (_REPO_ROOT / "docs/operator-runbook.md").read_text().lower()
    harnesses = _HARNESS_PATH.read_text().lower()
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    combined = "\n".join((readme, guide, operator, harnesses, html))

    for phrase in (
        "machinist doctor --run-gates",
        "machinist sync-labels --check",
        "current named stage",
        "last completed watcher poll",
        "python 3.12–3.14",
        "macos-only",
    ):
        assert phrase in combined
    assert "test-command auto-detection" not in combined
    assert "auto-detects test runners" not in combined
    assert "full-auto workspace edits" not in combined


def test_all_public_docs_share_current_approval_and_readiness_contract():
    trust = " ".join(_TRUST_MODEL_PATH.read_text().lower().split())
    operator = " ".join(
        (_REPO_ROOT / "docs/operator-runbook.md").read_text().lower().split()
    )
    first_run = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    job_card = " ".join(_JOB_CARD_PATH.read_text().lower().split())
    explainer = " ".join(_EXPLAINER_PATH.read_text().lower().split())

    assert (
        "both comment and label approval paths independently require write or admin access"
        in trust
    )
    assert (
        "inspect the managed approval workflow before requesting approval again"
        in operator
    )
    assert (
        "the managed workflow checks write or admin access and the current head"
        in job_card
    )
    assert "pi auth check --model &lt;model&gt; --json --no-refresh" in first_run
    assert "machinist sync-labels [--check|--apply]" in first_run
    assert "machinist doctor --run-gates — when" in job_card
    assert "machinist approve --issue 42" in explainer
    assert "machinist doctor --run-gates" in explainer

    combined = "\n".join((trust, operator, first_run, job_card, explainer))
    for stale_claim in (
        "guaranteed human-approved",
        "immutable spec pr",
        "machinist approve 42",
        "approved pr #18 bound",
        "automatically pruned upon test success",
        "ironclad checkpoint",
        "only from owners, members, or collaborators",
    ):
        assert stale_claim not in combined


def test_explainer_is_current_discoverable_and_has_page_metadata():
    index = (_REPO_ROOT / "docs/index.html").read_text().lower()
    explainer = _EXPLAINER_PATH.read_text().lower()

    assert 'href="explainer.html"' in index
    assert '<meta name="description"' in explainer
    assert '<meta name="theme-color"' in explainer
    assert '<link rel="canonical"' in explainer
    assert "reviewable pull requests" in explainer
    assert "failed runs retain" in explainer
    assert 'href="#main"' in explainer
    assert 'role="slider"' in explainer
    assert 'aria-pressed="true"' in explainer
    assert "prefers-reduced-motion" in explainer


def test_historical_design_records_are_clearly_labeled():
    for path in _HISTORICAL_DOCS:
        opening = "\n".join(path.read_text().splitlines()[:12]).lower()
        assert "historical design record" in opening, path
        assert "../../getting-started.md" in opening, path
        assert "../../architecture.md" in opening, path

    docs_index = _DOCS_INDEX_PATH.read_text().lower()
    assert "current operating documentation" in docs_index
    assert "historical design records" in docs_index
    assert "https://agentmachinist.vinny.dev/explainer.html" in docs_index


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
        "machinist update-check",
        "machinist onboard",
        "machinist rehearse",
        "machinist task new",
        "machinist task lint",
        "machinist review 42",
        "machinist explain 42",
        "machinist status --watch",
        "machinist report --since 30d",
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
        "review:",
        "telemetry:",
        "agentmachinist.harnesses.v1",
    ):
        assert setting in combined
    assert "runs `machinist watch --once`" in combined
    assert "uninstall" in operator and "preserves logs" in operator


def test_toolkit_expansion_docs_preserve_adoption_and_privacy_boundaries() -> None:
    readme = _README_PATH.read_text().lower()
    guide = _GUIDE_PATH.read_text().lower()
    harnesses = _HARNESS_PATH.read_text().lower()
    trust = " ".join(_TRUST_MODEL_PATH.read_text().lower().split())
    architecture = (_REPO_ROOT / "docs/architecture.md").read_text().lower()
    landing = (_REPO_ROOT / "docs/index.html").read_text().lower()
    first_run = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    job_card = _JOB_CARD_PATH.read_text().lower()
    explainer = _EXPLAINER_PATH.read_text().lower()

    assert "chore/agentmachinist-setup" in readme + guide
    assert "no model or api" in readme + guide
    assert ".github/issue_template/agentmachinist-task.yml" in readme + guide
    for name in ("claude code", "codex", "opencode", "pi"):
        assert name in harnesses
    assert "agentmachinist.harnesses.v1" in harnesses + architecture
    assert "findings are advisory" in guide
    assert "selected spec adapter" in first_run
    assert "machinist task new" in job_card
    assert "machinist review 7" in job_card
    assert "independent review" in explainer
    assert "machinist review 42" in explainer
    assert "machinist task new" in landing
    assert "machinist review 42" in landing
    assert "independent review" in landing
    for forbidden_export in (
        "issue bodies",
        "prompts",
        "diffs",
        "commands",
        "error messages",
        "credential values",
    ):
        assert forbidden_export in trust


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


def test_workflow_drift_advisory_is_documented_where_operators_read():
    """0.8.3 made watch and update-check report drift; the docs must say so.

    This suite already pins that documented commands exist in the CLI. It did
    not pin the other direction, which is how the advisory shipped
    undocumented. Asserting merely that "drift" and "sync-workflows" appear is
    not enough: both predate the change. The claim worth pinning is that drift
    reporting is tied to the two commands an operator runs after upgrading.
    """
    surfaces = {
        "README.md": _README_PATH,
        "docs/getting-started.md": _GUIDE_PATH,
        "docs/operator-runbook.md": _REPO_ROOT / "docs" / "operator-runbook.md",
    }
    for name, path in surfaces.items():
        lines = path.read_text().lower().splitlines()
        tied = [
            line
            for line in lines
            if "drift" in line and ("update-check" in line or "watch" in line)
        ]
        assert tied, f"{name} never says which command reports workflow drift"

    # The advisory must stay out of the scriptable output, and saying so is the
    # only thing that stops a future change from quietly breaking scripts.
    for name in ("README.md", "docs/operator-runbook.md"):
        text = surfaces[name].read_text().lower()
        assert "never appears in `update-check --json`" in text, name

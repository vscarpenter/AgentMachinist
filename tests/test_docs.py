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

import yaml

from machinist.cli import main
from machinist.config import LabelsConfig, MachinistConfig
from machinist.phases.status import PIPELINE_STATES

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GUIDE_PATH = _REPO_ROOT / "docs" / "getting-started.md"
_FIRST_RUN_GUIDE_PATH = _REPO_ROOT / "docs" / "first-run-guide.html"
_README_PATH = _REPO_ROOT / "README.md"
_CHANGELOG_PATH = _REPO_ROOT / "CHANGELOG.md"

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
    for command in (main, *main.commands.values()):
        for param in command.params:
            real_flags.update(
                opt for opt in (*param.opts, *param.secondary_opts) if opt.startswith("--")
            )
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
    assert labels.approved in text, f"guide never mentions the '{labels.approved}' label"


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
        for path in (_README_PATH, _GUIDE_PATH, *(_REPO_ROOT / "docs" / name for name in _REQUIRED_DOCS))
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


def test_visual_handbook_has_document_semantics_and_navigation():
    html = (_REPO_ROOT / "docs/onboarding.html").read_text().lower()
    for required in (
        "<!doctype html>",
        '<html lang="en">',
        '<meta name="viewport"',
        'href="#main"',
        '<nav aria-label=',
        '<main id="main"',
        "</html>",
    ):
        assert required in html


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
    assert "machinist approve 18" in html
    assert "machinist run 42" in html
    assert "/machinist-execute" in html
    assert "approval stale" in html
    assert "agentmachinist never" in html and "merge" in html
    assert (
        "https://github.com/vscarpenter/AgentMachinist/blob/main/docs/first-run-guide.html"
        in _README_PATH.read_text()
    )


def test_release_docs_describe_current_package_version():
    version = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    html = _FIRST_RUN_GUIDE_PATH.read_text().lower()
    assert f"agentmachinist {version} is available on pypi" in html
    assert "uv tool install agentmachinist" in html
    assert "unreleased" not in html
    changelog = _CHANGELOG_PATH.read_text()
    assert f"## {version} —" in changelog
    assert (
        f"https://pypi.org/project/agentmachinist/{version}/"
        in _README_PATH.read_text()
    )

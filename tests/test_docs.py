"""Drift tests: docs/getting-started.md must stay true to the code.

Guide conventions these tests rely on:
- Every ```yaml fenced block is a machinist.yaml snippet rooted at the
  top level, so it validates against MachinistConfig (extra="forbid").
- Prose never follows the word 'machinist' with a bare lowercase word
  unless it is a real subcommand (write 'AgentMachinist' or
  '.machinist/' instead).
"""

import re
from pathlib import Path

import yaml

from machinist.cli import main
from machinist.config import LabelsConfig, MachinistConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GUIDE_PATH = _REPO_ROOT / "docs" / "getting-started.md"
_README_PATH = _REPO_ROOT / "README.md"

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
    "## What's next (v0.1 limits)",
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
    assert "docs/getting-started.md" in _README_PATH.read_text()


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

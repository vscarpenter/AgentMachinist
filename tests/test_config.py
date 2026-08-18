"""Tests for machinist.yaml schema and loader."""

from pathlib import Path

import pytest

from machinist.config import (
    ConfigError,
    HarnessName,
    MachinistConfig,
    SpecInstall,
    load_config,
)


FULL_YAML = """\
version: 1
harness:
  name: codex
  command: /opt/bin/codex
  timeout_minutes: 45
  spec_timeout_minutes: 5
github:
  repo: vscarpenter/demo
  spec_source: github-actions
  spec_install: pypi
  labels:
    trigger: ai-task
    approved: "go:build"
  poll_interval_seconds: 120
workspace:
  root: ~/agents
  strategy: clone
  cleanup: never
  branch_prefix: bot/
tests:
  command: pytest -q
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "machinist.yaml"
    path.write_text(text)
    return path


def test_empty_file_yields_defaults(tmp_path):
    config = load_config(write_config(tmp_path, ""))
    assert config.harness.name is HarnessName.CLAUDE_CODE
    assert config.harness.timeout_minutes == 30
    assert config.github.labels.trigger == "agent-task"
    assert config.github.labels.approved == "machinist:approved"
    assert config.workspace.strategy.value == "worktree"
    assert config.tests.command is None


def test_full_config_round_trips(tmp_path):
    config = load_config(write_config(tmp_path, FULL_YAML))
    assert config.harness.name is HarnessName.CODEX
    assert config.harness.command == "/opt/bin/codex"
    assert config.harness.timeout_minutes == 45
    assert config.github.repo == "vscarpenter/demo"
    assert config.github.spec_source.value == "github-actions"
    assert config.github.labels.trigger == "ai-task"
    assert config.github.poll_interval_seconds == 120
    assert config.workspace.cleanup.value == "never"
    assert config.workspace.branch_prefix == "bot/"
    assert config.tests.command == "pytest -q"


def test_unknown_key_is_rejected(tmp_path):
    path = write_config(tmp_path, "harness:\n  timeout_minute: 60\n")
    with pytest.raises(ConfigError, match="timeout_minute"):
        load_config(path)


def test_invalid_harness_name_lists_valid_ones(tmp_path):
    path = write_config(tmp_path, "harness:\n  name: cursor\n")
    with pytest.raises(ConfigError, match="claude-code"):
        load_config(path)


def test_repo_must_be_owner_slash_name(tmp_path):
    path = write_config(tmp_path, "github:\n  repo: just-a-name\n")
    with pytest.raises(ConfigError, match="owner/repo"):
        load_config(path)


def test_timeout_out_of_bounds_is_rejected(tmp_path):
    path = write_config(tmp_path, "harness:\n  timeout_minutes: 0\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_file_mentions_init(tmp_path):
    with pytest.raises(ConfigError, match="machinist init"):
        load_config(tmp_path / "machinist.yaml")


def test_malformed_yaml_reports_path(tmp_path):
    path = write_config(tmp_path, "harness: [unclosed\n")
    with pytest.raises(ConfigError, match="machinist.yaml"):
        load_config(path)


def test_workspace_root_expands_home(tmp_path):
    config = load_config(write_config(tmp_path, "workspace:\n  root: ~/agents\n"))
    resolved = config.workspace.resolved_root()
    assert resolved.is_absolute()
    assert "~" not in str(resolved)


def test_defaults_construct_without_yaml():
    config = MachinistConfig()
    assert config.version == 1
    assert config.github.poll_interval_seconds == 60


def test_spec_install_defaults_to_pypi():
    config = MachinistConfig()
    assert config.github.spec_install is SpecInstall.PYPI


def test_unknown_spec_install_is_rejected(tmp_path):
    path = write_config(tmp_path, "github:\n  spec_install: docker\n")
    with pytest.raises(ConfigError, match="spec_install"):
        load_config(path)


@pytest.mark.parametrize("label", ["bad\nlabel", "bad'label", "", "x" * 51])
def test_workflow_labels_reject_unsafe_or_unusable_values(tmp_path, label):
    path = write_config(tmp_path, f"github:\n  labels:\n    trigger: {label!r}\n")
    with pytest.raises(ConfigError, match="label"):
        load_config(path)


@pytest.mark.parametrize("prefix", ["../agent/", "agent branch/", "/agent/", "agent//"])
def test_branch_prefix_rejects_unsafe_git_ref_shapes(tmp_path, prefix):
    path = write_config(tmp_path, f"workspace:\n  branch_prefix: {prefix!r}\n")
    with pytest.raises(ConfigError, match="branch_prefix"):
        load_config(path)

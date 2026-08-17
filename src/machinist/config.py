"""Schema and loader for machinist.yaml."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

CONFIG_FILENAME = "machinist.yaml"


class ConfigError(Exception):
    """A machinist.yaml problem the user must fix."""


class StrictModel(BaseModel):
    # Unknown keys in a hand-edited config file are almost always typos.
    model_config = ConfigDict(extra="forbid")


class HarnessName(str, Enum):
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    PI = "pi"
    CODEX = "codex"


class WorkspaceStrategy(str, Enum):
    WORKTREE = "worktree"
    CLONE = "clone"


class CleanupPolicy(str, Enum):
    ALWAYS = "always"
    ON_SUCCESS = "on_success"
    NEVER = "never"


class SpecSource(str, Enum):
    LOCAL = "local"
    GITHUB_ACTIONS = "github-actions"


class HarnessConfig(StrictModel):
    name: HarnessName = HarnessName.CLAUDE_CODE
    command: str | None = None
    timeout_minutes: int = Field(default=30, ge=1, le=240)
    spec_timeout_minutes: int = Field(default=10, ge=1, le=60)


class LabelsConfig(StrictModel):
    trigger: str = "agent-task"
    approved: str = "machinist:approved"


class GitHubConfig(StrictModel):
    repo: str | None = None
    spec_source: SpecSource = SpecSource.LOCAL
    labels: LabelsConfig = Field(default_factory=LabelsConfig)
    poll_interval_seconds: int = Field(default=60, ge=10)

    @field_validator("repo")
    @classmethod
    def _repo_shape(cls, value: str | None) -> str | None:
        if value is not None and value.count("/") != 1:
            raise ValueError("must look like 'owner/repo'")
        return value


class WorkspaceConfig(StrictModel):
    root: Path = Path("~/.machinist/workspaces")
    strategy: WorkspaceStrategy = WorkspaceStrategy.WORKTREE
    cleanup: CleanupPolicy = CleanupPolicy.ON_SUCCESS
    branch_prefix: str = "agent/"

    def resolved_root(self) -> Path:
        return self.root.expanduser().resolve()


class TestsConfig(StrictModel):
    command: str | None = None


class MachinistConfig(StrictModel):
    version: int = 1
    harness: HarnessConfig = Field(default_factory=HarnessConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    tests: TestsConfig = Field(default_factory=TestsConfig)


def load_config(path: str | Path = CONFIG_FILENAME) -> MachinistConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found. Run 'machinist init' first.")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return MachinistConfig.model_validate(data or {})
    except ValidationError as exc:
        raise ConfigError(f"{path} is invalid:\n{_format_errors(exc)}") from exc


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"  {location}: {error['msg']}")
    return "\n".join(lines)

"""Repository-local setup without a forge, prompts, or Harness invocation."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from machinist.config import (
    CONFIG_FILENAME,
    MAX_CONFIG_BYTES,
    ConfigError,
    HarnessPhase,
    MachinistConfig,
    SpecSource,
    WorkspaceConfig,
    harness_identifier,
    load_config,
)
from machinist.harness import HarnessRegistry, discover_harnesses
from machinist.local_workspace import LocalWorkspace
from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    open_regular_file,
    read_text_file,
    regular_file_exists,
)
from machinist.workspace import Workspace, WorkspaceError


def find_repository_root(cwd: Path) -> Path:
    """Resolve a Git working tree from any subdirectory, without an origin."""
    try:
        result = Workspace(cwd, WorkspaceConfig())._run(
            cwd, "rev-parse", "--show-toplevel"
        )
    except (WorkspaceError, OSError) as exc:
        raise ConfigError(f"cannot inspect Git repository: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise ConfigError(
            "current directory is not a Git working tree; "
            "run 'git init' or change to a repository"
        )
    return Path(result.stdout.strip()).resolve()


def load_local_config(repo_root: Path) -> MachinistConfig:
    """Read the bounded local configuration without changing any files."""
    try:
        runtime = _runtime(repo_root)
        path = runtime.path / "config.yaml"
        if not regular_file_exists(path):
            raise ConfigError(
                f"{path} not found. Run 'machinist start <task>' to configure local work."
            )
        config = load_config(path)
        _validate_local_config(config, runtime.repository_root, path)
        return config
    except RuntimePathError as exc:
        raise ConfigError(f"cannot safely read local configuration: {exc}") from exc


def ensure_local_config(
    repo_root: Path,
    *,
    harness_name: str | None = None,
    test_command: str | None = None,
) -> MachinistConfig:
    """Create local defaults once; preserve existing choices on subsequent starts."""
    try:
        runtime = _runtime(repo_root)
        path = runtime.path / "config.yaml"
        registry = discover_harnesses()
        if regular_file_exists(path):
            config = _existing_config(runtime, registry, harness_name, test_command)
        else:
            config = _new_config(
                runtime.repository_root,
                registry,
                harness_name=harness_name,
                test_command=test_command,
            )
        _validate_local_config(config, runtime.repository_root, path)
        LocalWorkspace(
            runtime.repository_root, config.workspace
        ).ensure_runtime_ignored()
        runtime.ensure(create=True)
        descriptor = open_regular_file(runtime.path / "setup.lock", truncate=False)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if regular_file_exists(path):
                return _existing_config(runtime, registry, harness_name, test_command)
            serialized = yaml.safe_dump(
                config.model_dump(mode="json", exclude_unset=True), sort_keys=False
            )
            if len(serialized.encode("utf-8")) > MAX_CONFIG_BYTES:
                raise ConfigError(
                    f"local configuration exceeds {MAX_CONFIG_BYTES} bytes"
                )
            atomic_write_text_file(path, serialized)
        finally:
            os.close(descriptor)
        return config
    except (RuntimePathError, WorkspaceError, ValidationError) as exc:
        raise ConfigError(f"cannot configure local work: {exc}") from exc


def _runtime(repo_root: Path) -> RuntimeDirectory:
    return RuntimeDirectory.bind(
        repo_root / ".machinist/runs/local", repo_root=repo_root
    )


def _existing_config(
    runtime: RuntimeDirectory,
    registry: HarnessRegistry,
    harness_name: str | None,
    test_command: str | None,
) -> MachinistConfig:
    config = load_local_config(runtime.repository_root)
    path = runtime.path / "config.yaml"
    if harness_name is not None and any(
        harness_identifier(config.harness_for(phase).name) != harness_name
        for phase in HarnessPhase
    ):
        raise ConfigError(
            f"--harness conflicts with {path}; edit that local configuration "
            "to change Harnesses, or omit --harness to reuse it"
        )
    if test_command is not None:
        gates = config.resolved_verification_gates()
        if not any(gate.required and gate.command == test_command for gate in gates):
            raise ConfigError(
                f"--test-cmd conflicts with {path}; edit that local configuration "
                "to change verification, or omit --test-cmd to reuse it"
            )
    _validate_harnesses(config, registry, runtime.repository_root)
    return config


def _new_config(
    root: Path,
    registry: HarnessRegistry,
    *,
    harness_name: str | None,
    test_command: str | None,
) -> MachinistConfig:
    source = root / CONFIG_FILENAME
    existing = load_config(source) if regular_file_exists(source) else None
    values = (
        existing.model_dump(mode="json", exclude_unset=True)
        if existing is not None
        else MachinistConfig.starter_projection(manage_workflows=False)
    )
    if harness_name is not None:
        selected = _select_harness(registry, harness_name)
        values["harness"] = _harness_override(existing, selected)
    elif existing is None or "harness" not in existing.model_fields_set:
        values["harness"] = {"name": _select_harness(registry, None)}
    values.setdefault("github", {}).update(
        {"spec_source": "local", "manage_workflows": False}
    )
    values.setdefault("telemetry", {})["otlp_endpoint"] = None
    values.setdefault("review", {})["enabled"] = True
    config = MachinistConfig.model_validate(values)
    workshop_root = config.workspace.root.expanduser()
    if not workshop_root.is_absolute():
        values.setdefault("workspace", {})["root"] = str(
            (root / workshop_root).resolve()
        )
    if test_command is not None:
        _set_test_command(values, config, test_command)
    elif not any(gate.required for gate in config.resolved_verification_gates()):
        detected = detect_test_command(root)
        if detected is not None:
            _set_test_command(values, config, detected)
        else:
            raise ConfigError(
                "No required verification gate was found. "
                "Pass --test-cmd '<command>' before starting a Task."
            )
    config = MachinistConfig.model_validate(values)
    _validate_harnesses(config, registry, root)
    return config


def _harness_override(existing: MachinistConfig | None, name: str) -> dict[str, Any]:
    if existing is None:
        return {"name": name}
    harness = existing.harness
    values = harness.model_dump(mode="json", exclude_unset=True)
    if harness_identifier(harness.name) != name:
        for field in ("command", "model", "extra_args"):
            values.pop(field, None)
    values["name"] = name
    for phase in HarnessPhase:
        if harness_identifier(existing.harness_for(phase).name) == name:
            continue
        profile = values.get(phase.value)
        if profile is None:
            continue
        for field in ("command", "model", "extra_args"):
            profile.pop(field, None)
        profile["name"] = name
    return values


def _set_test_command(
    values: dict[str, Any], config: MachinistConfig, command: str
) -> None:
    if not config.verification.gates:
        values["tests"] = {"command": command}
        return
    gates = values["verification"]["gates"]
    for gate in gates:
        if gate["command"] == command:
            gate["required"] = True
            return
    gates.append({"name": _test_gate_name(config), "command": command})


def _test_gate_name(config: MachinistConfig) -> str:
    names = {gate.name.casefold() for gate in config.verification.gates}
    index = 1
    while f"local-tests-{index}" in names:
        index += 1
    return f"local-tests-{index}"


def _select_harness(registry: HarnessRegistry, requested: str | None) -> str:
    phases = frozenset(phase.value for phase in HarnessPhase)
    if requested is not None:
        adapter = registry.adapters.get(requested)
        if adapter is None:
            raise ConfigError(
                f"unknown Harness '{requested}'; available: "
                + ", ".join(sorted(registry.adapters))
            )
        if not phases <= adapter.descriptor.phases:
            raise ConfigError(
                f"Harness '{requested}' must support all local phases: spec, execute, review"
            )
        return requested
    for name, adapter in registry.adapters.items():
        if phases <= adapter.descriptor.phases and shutil.which(
            adapter.default_command
        ):
            return name
    raise ConfigError(
        "No installed Harness supports Spec, Execute, and Review. Install and "
        "authenticate a supported Harness, then retry with --harness <name>."
    )


def _validate_harnesses(
    config: MachinistConfig, registry: HarnessRegistry, root: Path
) -> None:
    for phase in HarnessPhase:
        profile = config.harness_for(phase)
        name = harness_identifier(profile.name)
        adapter = registry.adapters.get(name)
        if adapter is None or phase.value not in adapter.descriptor.phases:
            raise ConfigError(f"configured Harness '{name}' cannot run {phase.value}")
        command = profile.command or adapter.default_command
        executable = str(root / command) if "/" in command else command
        if not shutil.which(executable):
            raise ConfigError(
                f"configured {phase.value} Harness executable '{command}' is not on PATH; "
                f"install and authenticate {adapter.descriptor.display_name}, then retry"
            )


def _validate_local_config(config: MachinistConfig, root: Path, path: Path) -> None:
    if (
        config.github.spec_source is not SpecSource.LOCAL
        or config.github.manage_workflows
    ):
        raise ConfigError(
            f"edit {path}: local work requires github.spec_source: local "
            "and github.manage_workflows: false"
        )
    if config.telemetry.otlp_endpoint is not None:
        raise ConfigError(
            f"edit {path}: local work requires telemetry.otlp_endpoint: null"
        )
    if not config.review.enabled:
        raise ConfigError(f"edit {path}: local work requires review.enabled: true")
    if not any(gate.required for gate in config.resolved_verification_gates()):
        raise ConfigError(
            f"edit {path}: local work requires a required verification gate"
        )
    if not config.workspace.root.expanduser().is_absolute():
        raise ConfigError(f"edit {path}: Workshop root must use an absolute path")
    if config.workspace.resolved_root().is_relative_to(root):
        raise ConfigError(f"edit {path}: Workshop root must be outside the repository")


def detect_test_command(root: Path) -> str | None:
    """Suggest only a manifest-backed test runner; never execute the command."""
    pyproject = _manifest(root / "pyproject.toml", toml=True)
    if pyproject is not None:
        tool = pyproject.get("tool", {})
        tool = tool if isinstance(tool, dict) else {}
        dependency_text = json.dumps(
            {
                "project": pyproject.get("project", {}),
                "dependency-groups": pyproject.get("dependency-groups", {}),
                "tool": {"uv": tool.get("uv", {})},
            },
            default=str,
        ).casefold()
        if tool.get("pytest") or re.search(r"\bpytest(?:\W|$)", dependency_text):
            return (
                "uv run pytest" if (root / "uv.lock").is_file() else "python -m pytest"
            )
    package = _manifest(root / "package.json", toml=False)
    scripts = package.get("scripts", {}) if package is not None else {}
    script = scripts.get("test") if isinstance(scripts, dict) else None
    if (
        isinstance(script, str)
        and script.strip()
        and "no test specified" not in script.casefold()
    ):
        for lock, command in (
            ("bun.lock", "bun run test"),
            ("bun.lockb", "bun run test"),
            ("pnpm-lock.yaml", "pnpm test"),
            ("yarn.lock", "yarn test"),
        ):
            if (root / lock).is_file():
                return command
        return "npm test"
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "go.mod").is_file():
        return "go test ./..."
    return None


def _manifest(path: Path, *, toml: bool) -> dict[str, Any] | None:
    try:
        if not regular_file_exists(path):
            return None
        text = read_text_file(path, max_bytes=MAX_CONFIG_BYTES)
        data = tomllib.loads(text) if toml else json.loads(text)
    except (RuntimePathError, OSError, UnicodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None

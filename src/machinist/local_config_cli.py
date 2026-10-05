"""Inspect and safely edit the saved settings used by foreground local Tasks.

Selection and terminal rendering belong to the CLI. Local workflow validation
remains owned by ``local_setup``; these helpers never configure a repository or
invoke a Harness while inspecting or changing its saved values.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from machinist.config import (
    MAX_CONFIG_BYTES,
    ConfigError,
    MachinistConfig,
    read_config_text,
    strict_yaml_load,
)
from machinist.local_setup import _validate_local_config, load_local_config
from machinist.runtime_paths import (
    RuntimeDirectory,
    RuntimePathError,
    atomic_write_text_file,
    open_regular_file,
    regular_file_exists,
)

LOCAL_CONFIG_CHANGE_NOTICE = (
    "Saved local settings apply to new and existing local Tasks the next time "
    "a foreground command loads configuration. A running command keeps the "
    "settings it already loaded; completed Phase evidence is unchanged."
)


def local_config_path(repo_root: Path) -> Path:
    """Return the safe saved-settings path without creating runtime state."""
    path = _runtime(repo_root).path / "config.yaml"
    try:
        regular_file_exists(path)
    except RuntimePathError as exc:
        raise ConfigError(f"cannot safely read local configuration: {exc}") from exc
    return path


def show_local_effective(repo_root: Path) -> dict[str, Any]:
    """Show the saved, Phase-resolved values with their local workflow source.

    Inspection deliberately does not probe installed Harnesses. Settings can
    be inspected and repaired even when the selected executable is unavailable.
    """
    runtime = _runtime(repo_root)
    config = load_local_config(runtime.repository_root)
    return {
        "configuration": {
            "workflow": "local",
            "source": "saved",
            "path": str(runtime.path / "config.yaml"),
            "changes_apply": LOCAL_CONFIG_CHANGE_NOTICE,
        },
        **config.effective_projection(),
    }


def set_local_value(
    dotted_key: str,
    value_text: str,
    repo_root: Path,
) -> MachinistConfig:
    """Validate a saved local edit before safely replacing canonical YAML.

    Both the strict configuration schema and the local workflow invariants are
    checked before any write. The setup lock coordinates replacement with local
    setup; a changed source is refused rather than overwriting a concurrent edit.
    """
    runtime = _runtime(repo_root)
    path = runtime.path / "config.yaml"
    # Keep authoritative setup guidance for an absent saved file. Existing
    # settings are validated after the edit so an operator can repair a value
    # that currently violates the schema or local workflow requirements.
    try:
        if not regular_file_exists(path):
            load_local_config(runtime.repository_root)
    except RuntimePathError as exc:
        raise ConfigError(f"cannot safely read local configuration: {exc}") from exc
    source = read_config_text(path)
    config, payload = _updated_configuration(
        dotted_key, value_text, source, runtime.repository_root, path
    )
    descriptor = -1
    try:
        descriptor = open_regular_file(runtime.path / "setup.lock", truncate=False)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if read_config_text(path) != source:
            raise ConfigError("local configuration changed during the update; retry")
        atomic_write_text_file(path, payload)
    except (RuntimePathError, OSError) as exc:
        raise ConfigError(f"cannot safely update local configuration: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return config


def _runtime(repo_root: Path) -> RuntimeDirectory:
    try:
        return RuntimeDirectory.bind(
            repo_root / ".machinist/runs/local", repo_root=repo_root
        )
    except (RuntimePathError, OSError) as exc:
        raise ConfigError(f"cannot safely read local configuration: {exc}") from exc


def _updated_configuration(
    dotted_key: str,
    value_text: str,
    source: str,
    repo_root: Path,
    path: Path,
) -> tuple[MachinistConfig, str]:
    parts = dotted_key.split(".")
    if not parts or any(not part or part.startswith("_") for part in parts):
        raise ConfigError("config key must be a dotted public field name")
    if len(value_text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"local configuration exceeds {MAX_CONFIG_BYTES} bytes")
    try:
        raw = strict_yaml_load(source)
        value = strict_yaml_load(value_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"cannot update {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    cursor = raw
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, dict):
            raise ConfigError(f"cannot set {dotted_key}: {part} is not a mapping")
        cursor = child
    cursor[parts[-1]] = value
    try:
        config = MachinistConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"refusing invalid config update: {exc}") from exc
    _validate_local_config(config, repo_root, path)
    payload = yaml.safe_dump(raw, sort_keys=False)
    if len(payload.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ConfigError(f"local configuration exceeds {MAX_CONFIG_BYTES} bytes")
    return config, payload

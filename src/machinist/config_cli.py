"""Presentation-neutral helpers for ``machinist config`` commands.

The CLI owns terminal formatting and exit codes.  This module keeps config
loading, effective-value normalization, and schema output independently
testable so other integrations can use the same behavior.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from machinist.config import (
    CONFIG_FILENAME,
    ConfigError,
    HarnessPhase,
    MachinistConfig,
    config_json_schema,
    load_config,
    read_config_text,
    strict_yaml_load,
)


class ConfigValidationErrorKind(str, Enum):
    """Stable machine-readable categories for config validation failures."""

    NOT_FOUND = "not_found"
    YAML = "yaml"
    VALIDATION = "validation"
    IO = "io"


@dataclass(frozen=True)
class ConfigValidationError:
    """A config validation failure safe to render without a traceback."""

    kind: ConfigValidationErrorKind
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON- and YAML-safe error payload."""
        return {
            "kind": self.kind.value,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class ConfigValidationResult:
    """Structured success or failure returned by :func:`validate`."""

    path: str
    config: MachinistConfig | None = None
    error: ConfigValidationError | None = None

    def __post_init__(self) -> None:
        if (self.config is None) == (self.error is None):
            raise ValueError("a validation result must contain config or error")

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def valid(self) -> bool:
        """Readable alias for callers that prefer ``result.valid``."""
        return self.ok

    def as_dict(self) -> dict[str, Any]:
        """Return validation status without serializing the config model."""
        return {
            "ok": self.ok,
            "path": self.path,
            "error": self.error.as_dict() if self.error is not None else None,
        }

    def require(self) -> MachinistConfig:
        """Return the loaded config or raise its user-facing ``ConfigError``."""
        if self.config is not None:
            return self.config
        assert self.error is not None  # Protected by __post_init__.
        raise ConfigError(self.error.message)


def validate(path: str | Path = CONFIG_FILENAME) -> ConfigValidationResult:
    """Validate one config file and return data instead of ``ConfigError``."""
    config_path = Path(path)
    display_path = str(config_path)
    try:
        if not config_path.exists():
            return _failure(
                display_path,
                ConfigValidationErrorKind.NOT_FOUND,
                f"{display_path} not found. Run 'machinist init' first.",
            )
        if not config_path.is_file():
            return _failure(
                display_path,
                ConfigValidationErrorKind.IO,
                f"{display_path} is not a regular file.",
            )
        config = load_config(config_path)
    except OSError as exc:
        kind = (
            ConfigValidationErrorKind.NOT_FOUND
            if isinstance(exc, FileNotFoundError)
            else ConfigValidationErrorKind.IO
        )
        return _failure(display_path, kind, f"cannot read {display_path}: {exc}")
    except ConfigError as exc:
        cause = exc.__cause__
        if isinstance(cause, yaml.YAMLError):
            kind = ConfigValidationErrorKind.YAML
        elif isinstance(cause, ValidationError):
            kind = ConfigValidationErrorKind.VALIDATION
        else:
            # ConfigError is the validation boundary exposed by config.py.  A
            # future loader-side validation check should remain fail-closed
            # even if it does not use Pydantic as its direct cause.
            kind = ConfigValidationErrorKind.VALIDATION
        return _failure(display_path, kind, str(exc))
    return ConfigValidationResult(path=display_path, config=config)


def show_effective(path: str | Path = CONFIG_FILENAME) -> dict[str, Any]:
    """Return a canonical JSON/YAML-safe view of effective runtime config.

    Legacy shared harness values and ``tests.command`` are normalized to the
    phase-specific harness and named-gate shapes.  The returned mapping is for
    presentation and inspection; it contains no Pydantic, Enum, or Path values.
    """
    config = load_config(path)
    effective = config.model_dump(mode="json")
    effective["harness"] = {
        phase.value: _effective_harness(config, phase) for phase in HarnessPhase
    }
    effective["workspace"]["root"] = str(config.workspace.resolved_root())
    effective["verification"] = {
        "gates": [
            gate.model_dump(mode="json")
            for gate in config.resolved_verification_gates()
        ]
    }
    # The legacy field has been represented by verification.gates above.  Its
    # removal prevents an effective view from containing contradictory inputs.
    effective.pop("tests", None)
    return effective


def schema() -> dict[str, Any]:
    """Return AgentMachinist's generated JSON Schema as a safe mapping."""
    return config_json_schema()


def write_schema(path: str | Path, *, indent: int = 2) -> Path:
    """Atomically write the generated JSON Schema and return its target path."""
    if indent < 0:
        raise ValueError("indent must be zero or greater")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            schema(),
            allow_nan=False,
            indent=indent,
            sort_keys=True,
        )
        + "\n"
    )

    mode = 0o644
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, text=True
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def set_value(
    dotted_key: str,
    value_text: str,
    path: str | Path = CONFIG_FILENAME,
) -> MachinistConfig:
    """Validate and atomically set one dotted config value.

    The command deliberately rewrites the file as canonical YAML. Callers
    should show that fact in help text because YAML comments are not retained.
    """
    parts = dotted_key.split(".")
    if not parts or any(not part or part.startswith("_") for part in parts):
        raise ConfigError("config key must be a dotted public field name")
    target = Path(path)
    try:
        raw = strict_yaml_load(read_config_text(target))
        value = strict_yaml_load(value_text)
    except FileNotFoundError as exc:
        raise ConfigError(f"{target} not found. Run 'machinist init' first.") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot update {target}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} must contain a YAML mapping")

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

    payload = yaml.safe_dump(raw, sort_keys=False)
    mode = stat.S_IMODE(target.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, text=True
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return config


def _failure(
    path: str,
    kind: ConfigValidationErrorKind,
    message: str,
) -> ConfigValidationResult:
    return ConfigValidationResult(
        path=path,
        error=ConfigValidationError(kind=kind, path=path, message=message),
    )


def _effective_harness(
    config: MachinistConfig,
    phase: HarnessPhase,
) -> dict[str, Any]:
    harness = config.harness_for(phase)
    timeout = (
        harness.spec_timeout_minutes
        if phase in {HarnessPhase.SPEC, HarnessPhase.REVIEW}
        else harness.timeout_minutes
    )
    return {
        "name": harness.name.value,
        "command": harness.command,
        "model": harness.model,
        "extra_args": list(harness.extra_args),
        "timeout_minutes": timeout,
    }

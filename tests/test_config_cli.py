"""Tests for presentation-neutral ``machinist config`` helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from machinist.config import ConfigError, HarnessName
from machinist.config_cli import (
    ConfigValidationErrorKind,
    ConfigValidationResult,
    schema,
    set_value,
    show_effective,
    validate,
    write_schema,
)


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "machinist.yaml"
    path.write_text(text)
    return path


def test_validate_returns_loaded_config_and_safe_result(tmp_path):
    path = write_config(tmp_path, "version: 1\n")

    result = validate(path)

    assert result.ok
    assert result.valid
    assert result.error is None
    assert result.config is not None
    assert result.config.harness.name is HarnessName.CLAUDE_CODE
    assert result.require() is result.config
    assert result.as_dict() == {
        "ok": True,
        "path": str(path),
        "error": None,
    }
    json.dumps(result.as_dict(), allow_nan=False)
    yaml.safe_dump(result.as_dict())


def test_validate_returns_structured_not_found_error(tmp_path):
    path = tmp_path / "missing.yaml"

    result = validate(path)

    assert not result.ok
    assert not result.valid
    assert result.config is None
    assert result.error is not None
    assert result.error.kind is ConfigValidationErrorKind.NOT_FOUND
    assert result.error.path == str(path)
    assert "machinist init" in result.error.message
    assert result.as_dict()["error"]["kind"] == "not_found"
    with pytest.raises(ConfigError, match="machinist init"):
        result.require()


def test_validate_classifies_malformed_yaml(tmp_path):
    path = write_config(tmp_path, "harness: [unclosed\n")

    result = validate(path)

    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ConfigValidationErrorKind.YAML
    assert str(path) in result.error.message


def test_validate_classifies_schema_validation(tmp_path):
    path = write_config(tmp_path, "version: 2\n")

    result = validate(path)

    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ConfigValidationErrorKind.VALIDATION
    assert "version" in result.error.message


def test_validate_classifies_non_file_as_io_error(tmp_path):
    result = validate(tmp_path)

    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ConfigValidationErrorKind.IO
    assert "not a regular file" in result.error.message


def test_validation_result_requires_exactly_one_outcome():
    with pytest.raises(ValueError, match="config or error"):
        ConfigValidationResult(path="machinist.yaml")


def test_show_effective_normalizes_legacy_config_and_is_serializable(tmp_path):
    path = write_config(
        tmp_path,
        """\
version: 1
harness:
  name: codex
  command: /opt/codex
  model: shared-model
  extra_args: [--verbose]
  spec_timeout_minutes: 7
  timeout_minutes: 45
workspace:
  root: ~/agents
tests:
  command: pytest -q
""",
    )

    effective = show_effective(path)

    assert effective["version"] == 1
    assert effective["harness"] == {
        "spec": {
            "name": "codex",
            "command": "/opt/codex",
            "model": "shared-model",
            "extra_args": ["--verbose"],
            "timeout_minutes": 7,
        },
        "execute": {
            "name": "codex",
            "command": "/opt/codex",
            "model": "shared-model",
            "extra_args": ["--verbose"],
            "timeout_minutes": 45,
        },
    }
    assert effective["verification"]["gates"] == [
        {
            "name": "legacy-tests",
            "command": "pytest -q",
            "timeout_minutes": 45,
            "required": True,
            "mutation_policy": "allow",
        }
    ]
    assert "tests" not in effective
    assert Path(effective["workspace"]["root"]).is_absolute()
    assert effective["queue"]["max_tasks_per_pass"] == 1
    assert effective["limits"]["allow_binary"] is False
    json.dumps(effective, allow_nan=False)
    yaml.safe_dump(effective)


def test_show_effective_resolves_phase_profiles(tmp_path):
    path = write_config(
        tmp_path,
        """\
harness:
  name: codex
  command: /opt/codex
  spec:
    name: claude-code
    model: fast
    timeout_minutes: 4
  execute:
    model: strong
    timeout_minutes: 90
""",
    )

    effective = show_effective(path)

    assert effective["harness"]["spec"] == {
        "name": "claude-code",
        "command": None,
        "model": "fast",
        "extra_args": [],
        "timeout_minutes": 4,
    }
    assert effective["harness"]["execute"] == {
        "name": "codex",
        "command": "/opt/codex",
        "model": "strong",
        "extra_args": [],
        "timeout_minutes": 90,
    }


def test_show_effective_uses_existing_config_error_contract(tmp_path):
    path = write_config(tmp_path, "version: 9\n")

    with pytest.raises(ConfigError, match="version"):
        show_effective(path)


def test_schema_is_generated_and_serializable():
    generated = schema()

    assert generated["type"] == "object"
    assert generated["properties"]["version"]["const"] == 1
    assert "harness" in generated["properties"]
    assert "verification" in generated["properties"]
    json.dumps(generated, allow_nan=False)
    yaml.safe_dump(generated)


def test_write_schema_creates_parent_and_writes_parseable_json(tmp_path):
    target = tmp_path / "generated" / "machinist.schema.json"

    returned = write_schema(target)

    assert returned == target
    assert target.read_text().endswith("\n")
    assert json.loads(target.read_text()) == schema()
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_write_schema_replaces_existing_file_and_preserves_mode(tmp_path):
    target = tmp_path / "machinist.schema.json"
    target.write_text("stale\n")
    target.chmod(0o640)

    write_schema(target, indent=0)

    assert json.loads(target.read_text()) == schema()
    assert target.stat().st_mode & 0o777 == 0o640


def test_write_schema_rejects_negative_indent(tmp_path):
    with pytest.raises(ValueError, match="indent"):
        write_schema(tmp_path / "schema.json", indent=-1)


def test_set_value_updates_nested_field_and_validates_result(tmp_path):
    path = write_config(tmp_path, "version: 1\ngithub:\n  poll_interval_seconds: 60\n")

    config = set_value("github.poll_interval_seconds", "120", path)

    assert config.github.poll_interval_seconds == 120
    assert yaml.safe_load(path.read_text())["github"]["poll_interval_seconds"] == 120


def test_set_value_refuses_invalid_or_unknown_update_without_replacing_file(tmp_path):
    path = write_config(tmp_path, "version: 1\n")
    original = path.read_text()

    with pytest.raises(ConfigError, match="invalid config update"):
        set_value("github.poll_interval_seconds", "1", path)
    with pytest.raises(ConfigError, match="invalid config update"):
        set_value("unknown.field", "true", path)

    assert path.read_text() == original

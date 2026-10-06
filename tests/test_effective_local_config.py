"""Saved local settings are explicit, effective, and safely editable."""

from pathlib import Path

import pytest
import yaml

from machinist.config import ConfigError
from machinist.config_cli import show_effective
from machinist.local_config_cli import (
    LOCAL_CONFIG_CHANGE_NOTICE,
    local_config_path,
    set_local_value,
    show_local_effective,
)
from machinist.local_setup import load_local_config


def saved_configuration(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repository"
    root.mkdir()
    path = root / ".machinist/runs/local/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "harness": {
                    "name": "codex",
                    "model": "saved-local-model",
                    "review": {"model": "independent-review-model"},
                },
                "github": {"spec_source": "local", "manage_workflows": False},
                "tests": {"command": "python -m pytest"},
                "workspace": {"root": str(tmp_path / "workshops")},
                "review": {"enabled": True},
            }
        )
    )
    return root, path


def test_explicit_local_effective_view_uses_saved_settings_with_root_present(
    tmp_path, monkeypatch
):
    from machinist import local_setup

    def forbidden_probe():
        raise AssertionError("config inspection must not discover or invoke a Harness")

    monkeypatch.setattr(local_setup, "discover_harnesses", forbidden_probe)
    root, saved = saved_configuration(tmp_path)
    root_path = root / "machinist.yaml"
    root_path.write_text("harness:\n  name: claude-code\n  model: root-model\n")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

    effective = show_local_effective(root)

    assert effective["configuration"]["workflow"] == "local"
    assert effective["configuration"]["source"] == "saved"
    assert effective["configuration"]["path"] == str(saved)
    assert effective["configuration"]["changes_apply"] == LOCAL_CONFIG_CHANGE_NOTICE
    assert effective["harness"]["execute"]["name"] == "codex"
    assert effective["harness"]["execute"]["model"] == "saved-local-model"
    assert effective["harness"]["review"]["model"] == "independent-review-model"
    assert effective["verification"]["gates"][0]["command"] == "python -m pytest"
    assert show_effective(root_path)["harness"]["execute"]["model"] == "root-model"
    assert before == {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_missing_local_configuration_read_and_path_lookup_create_no_state(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()

    assert local_config_path(root) == root / ".machinist/runs/local/config.yaml"
    with pytest.raises(ConfigError, match="machinist start"):
        show_local_effective(root)

    assert list(root.iterdir()) == []
    with pytest.raises(ConfigError, match="machinist start"):
        set_local_value("harness.model", "unsaved", root)
    assert list(root.iterdir()) == []


def test_saved_edit_changes_next_local_command_and_leaves_root_and_active_values(
    tmp_path, monkeypatch
):
    from machinist import local_cli

    root, saved = saved_configuration(tmp_path)
    root_path = root / "machinist.yaml"
    root_path.write_text("harness:\n  model: unchanged-root-model\n")
    root_bytes = root_path.read_bytes()
    monkeypatch.setattr(local_cli, "find_repository_root", lambda cwd: root)
    monkeypatch.setattr(local_cli, "_workflow", lambda config, repository: config)
    active_config = local_cli._existing_workflow()

    updated = set_local_value("harness.model", "next-command-model", root)
    next_command_config = local_cli._existing_workflow()

    assert active_config.harness_for("execute").model == "saved-local-model"
    assert updated.harness_for("execute").model == "next-command-model"
    assert next_command_config.harness_for("execute").model == "next-command-model"
    assert load_local_config(root).harness_for("execute").model == "next-command-model"
    assert root_path.read_bytes() == root_bytes
    assert "next-command-model" in saved.read_text()
    assert "existing" in LOCAL_CONFIG_CHANGE_NOTICE
    assert "next" in LOCAL_CONFIG_CHANGE_NOTICE
    assert "running" in LOCAL_CONFIG_CHANGE_NOTICE


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("review.enabled", "false"),
        ("github.manage_workflows", "true"),
        ("github.spec_source", "github-actions"),
        ("telemetry.otlp_endpoint", "https://example.invalid/otlp"),
        ("tests.command", "null"),
        ("workspace.root", "relative-workshops"),
        ("limits.unknown", "true"),
        ("harness.extra_args", "[--dangerously-bypass-approvals-and-sandbox]"),
    ],
)
def test_invalid_or_incompatible_local_edit_does_not_mutate_config(
    tmp_path, key, value
):
    root, saved = saved_configuration(tmp_path)
    before = saved.read_bytes()

    with pytest.raises(ConfigError):
        set_local_value(key, value, root)

    assert saved.read_bytes() == before
    assert list(saved.parent.glob(".*.tmp")) == []


@pytest.mark.parametrize("symlink_parent", [False, True])
def test_local_edit_rejects_unsafe_saved_path_without_mutating_target(
    tmp_path, symlink_parent
):
    root, saved = saved_configuration(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    outside = external / "config.yaml"
    outside.write_bytes(saved.read_bytes())
    before = outside.read_bytes()
    saved.unlink()
    if symlink_parent:
        saved.parent.rmdir()
        saved.parent.symlink_to(external, target_is_directory=True)
    else:
        saved.symlink_to(outside)

    with pytest.raises(ConfigError, match="safely|unsafe"):
        local_config_path(root)
    with pytest.raises(ConfigError, match="safely|unsafe"):
        show_local_effective(root)
    with pytest.raises(ConfigError, match="safely|unsafe"):
        set_local_value("harness.model", "should-not-be-saved", root)

    assert outside.read_bytes() == before


def test_local_edit_refuses_oversized_update_without_mutation(tmp_path):
    from machinist.config import MAX_CONFIG_BYTES

    root, saved = saved_configuration(tmp_path)
    before = saved.read_bytes()

    with pytest.raises(ConfigError, match="exceeds"):
        set_local_value("instructions.common.text", "x" * (MAX_CONFIG_BYTES + 1), root)

    assert saved.read_bytes() == before


def test_local_edit_can_repair_an_existing_incompatible_value(tmp_path):
    root, saved = saved_configuration(tmp_path)
    values = yaml.safe_load(saved.read_text())
    values["review"]["enabled"] = False
    saved.write_text(yaml.safe_dump(values))
    with pytest.raises(ConfigError, match="review.enabled"):
        load_local_config(root)

    set_local_value("review.enabled", "true", root)

    assert load_local_config(root).review.enabled


def test_duplicate_yaml_value_and_private_key_fail_without_creating_lock(tmp_path):
    root, saved = saved_configuration(tmp_path)
    before = saved.read_bytes()

    for key, value in (("harness", "name: codex\nname: pi\n"), ("_private", "true")):
        with pytest.raises(ConfigError):
            set_local_value(key, value, root)

    assert saved.read_bytes() == before
    assert not (saved.parent / "setup.lock").exists()


def test_local_edit_rejects_unsafe_setup_lock_without_replacing_saved_values(tmp_path):
    root, saved = saved_configuration(tmp_path)
    outside = tmp_path / "outside-lock"
    outside.write_text("outside is unchanged")
    (saved.parent / "setup.lock").symlink_to(outside)
    before = saved.read_bytes()

    with pytest.raises(ConfigError, match="safely"):
        set_local_value("harness.model", "different", root)

    assert outside.read_text() == "outside is unchanged"
    assert saved.read_bytes() == before


def test_local_edit_refuses_a_source_changed_while_waiting_for_lock(
    tmp_path, monkeypatch
):
    from machinist import local_config_cli

    root, saved = saved_configuration(tmp_path)
    original_open = local_config_cli.open_regular_file
    concurrent = saved.read_text().replace("saved-local-model", "concurrent-model")

    def change_then_open(path, **kwargs):
        saved.write_text(concurrent)
        return original_open(path, **kwargs)

    monkeypatch.setattr(local_config_cli, "open_regular_file", change_then_open)

    with pytest.raises(ConfigError, match="changed.*retry"):
        set_local_value("harness.model", "overwriting-model", root)

    assert saved.read_text() == concurrent


def test_failed_local_atomic_write_preserves_saved_settings(tmp_path, monkeypatch):
    from machinist import local_config_cli
    from machinist.runtime_paths import RuntimePathError

    root, saved = saved_configuration(tmp_path)
    before = saved.read_bytes()

    def fail_write(path, value):
        raise RuntimePathError("simulated storage failure")

    monkeypatch.setattr(local_config_cli, "atomic_write_text_file", fail_write)

    with pytest.raises(ConfigError, match="simulated storage failure"):
        set_local_value("harness.model", "not-written", root)

    assert saved.read_bytes() == before

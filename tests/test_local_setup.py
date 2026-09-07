"""Local setup works without a forge and never rewrites project files."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from machinist.config import MAX_CONFIG_BYTES, ConfigError, SpecSource
from machinist.harness import HarnessRegistry
from machinist.harness.codex import Codex
from machinist.local_setup import (
    detect_test_command,
    ensure_local_config,
    find_repository_root,
    load_local_config,
    resolve_local_config,
)


class LocalPlugin(Codex):
    name = "local-plugin"
    default_command = "local-agent"
    descriptor = replace(Codex.descriptor, ci_spec=None)


class SpecOnly(LocalPlugin):
    name = "spec-only"
    descriptor = replace(LocalPlugin.descriptor, phases=frozenset({"spec"}))


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Local Developer")
    git(root, "config", "user.email", "local@example.com")
    (root / "README.md").write_text("baseline\n")
    git(root, "add", "README.md")
    git(root, "commit", "-qm", "baseline")
    monkeypatch.setattr(
        "machinist.local_setup.discover_harnesses",
        lambda: HarnessRegistry(
            {"spec-only": SpecOnly, "codex": Codex, "local-plugin": LocalPlugin}
        ),
    )
    monkeypatch.setattr(
        "machinist.local_setup.shutil.which",
        lambda command: "/bin/local-agent" if command == "local-agent" else None,
    )
    return root


def local_path(repo: Path) -> Path:
    return repo / ".machinist/runs/local/config.yaml"


def test_read_only_resolution_matches_first_start_without_creating_runtime(repo):
    exclude = (repo / ".git/info/exclude").read_bytes()
    config = resolve_local_config(repo, test_command="python -m pytest")

    assert not (repo / ".machinist").exists()
    assert (repo / ".git/info/exclude").read_bytes() == exclude
    assert ensure_local_config(repo, test_command="python -m pytest") == config


def test_read_only_resolution_reuses_local_choices_and_injected_path_lookup(repo):
    expected = ensure_local_config(repo, test_command="pytest")
    before = local_path(repo).read_bytes()
    (repo / "machinist.yaml").write_text("invalid: root config\n")
    calls = []

    def which(command):
        calls.append(command)
        return "/bin/local-agent" if command == "local-agent" else None

    assert resolve_local_config(repo, which=which) == expected
    assert calls == ["local-agent"] * 3
    assert local_path(repo).read_bytes() == before


def test_first_setup_detects_installed_plugin_and_keeps_project_unchanged(repo):
    before = git(repo, "status", "--porcelain")
    config = ensure_local_config(repo, test_command="python -m pytest")

    assert config.harness.name == "local-plugin"
    assert config.review.enabled
    assert not config.github.manage_workflows
    assert config.github.spec_source is SpecSource.LOCAL
    assert config.tests.command == "python -m pytest"
    assert not config.workspace.resolved_root().is_relative_to(repo)
    assert git(repo, "status", "--porcelain") == before
    assert git(repo, "remote") == ""
    assert not (repo / "machinist.yaml").exists()
    assert not (repo / ".gitignore").exists()
    assert local_path(repo).stat().st_mode & 0o777 == 0o600
    assert load_local_config(repo) == config


def test_setup_requires_a_gate_before_creating_runtime_or_ignore_entries(repo):
    exclude = repo / ".git/info/exclude"
    before = exclude.read_bytes()
    with pytest.raises(ConfigError, match="required verification.*--test-cmd"):
        ensure_local_config(repo)
    assert not local_path(repo).exists()
    assert exclude.read_bytes() == before


def test_existing_runtime_is_preserved_verbatim_and_ignores_later_root_defaults(repo):
    config = ensure_local_config(repo, test_command="pytest")
    runtime = local_path(repo)
    runtime.write_text("# user's local choices\n" + runtime.read_text())
    before = runtime.read_bytes()
    (repo / "machinist.yaml").write_text("invalid: root config\n")

    assert ensure_local_config(repo, test_command="pytest") == config
    assert runtime.read_bytes() == before


@pytest.mark.parametrize(
    "options", [{"harness_name": "codex"}, {"test_command": "other"}]
)
def test_conflicting_runtime_flags_point_to_preserved_local_config(repo, options):
    ensure_local_config(repo, test_command="pytest")
    before = local_path(repo).read_bytes()
    with pytest.raises(ConfigError) as error:
        ensure_local_config(repo, **options)
    assert str(local_path(repo)) in str(error.value)
    assert "edit" in str(error.value).lower()
    assert local_path(repo).read_bytes() == before


def test_root_defaults_are_preserved_and_only_remote_settings_are_localized(repo):
    source = (
        "harness:\n  name: local-plugin\n  model: custom\n"
        "github:\n  spec_source: github-actions\n  manage_workflows: true\n"
        "verification:\n  gates:\n    - name: contract\n      command: pytest\n"
        "review:\n  enabled: false\n"
        "instructions:\n  execute:\n    append: Preserve the public API.\n"
    )
    (repo / "machinist.yaml").write_text(source)
    config = ensure_local_config(repo)

    assert config.harness.model == "custom"
    assert config.resolved_verification_gates()[0].name == "contract"
    assert config.instructions.execute.append == "Preserve the public API."
    assert config.github.spec_source is SpecSource.LOCAL
    assert not config.github.manage_workflows
    assert config.review.enabled
    assert (repo / "machinist.yaml").read_text() == source


def test_phase_inheritance_survives_local_config_round_trip(repo):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: local-plugin\n  model: shared-model\n"
        "  extra_args: [--verbose]\n  execute:\n    timeout_minutes: 45\n"
    )
    created = ensure_local_config(repo, test_command="pytest")
    loaded = load_local_config(repo)
    assert loaded.harness_for("execute").model == "shared-model"
    assert loaded.harness_for("execute").extra_args == ["--verbose"]
    assert loaded.harness_for("execute") == created.harness_for("execute")


def test_local_projection_disables_inherited_network_telemetry(repo):
    (repo / "machinist.yaml").write_text(
        "telemetry:\n  otlp_endpoint: https://example.com/metrics\n"
    )
    assert (
        ensure_local_config(repo, test_command="pytest").telemetry.otlp_endpoint is None
    )


def test_missing_harness_raises_actionable_error_before_runtime_write(
    repo, monkeypatch
):
    monkeypatch.setattr("machinist.local_setup.shutil.which", lambda command: None)
    with pytest.raises(ConfigError, match="[Ii]nstall.*--harness"):
        ensure_local_config(repo, test_command="pytest")
    assert not local_path(repo).exists()


@pytest.mark.parametrize(
    "name, message",
    [("missing", "unknown"), ("spec-only", "phases"), ("codex", "PATH")],
)
def test_explicit_harness_must_be_known_installed_and_full_pipeline(
    repo, name, message
):
    with pytest.raises(ConfigError, match=message):
        ensure_local_config(repo, harness_name=name, test_command="pytest")
    assert not local_path(repo).exists()


def test_load_does_not_require_an_installed_harness_or_modify_files(repo, monkeypatch):
    config = ensure_local_config(repo, test_command="pytest")
    before = local_path(repo).read_bytes()
    monkeypatch.setattr("machinist.local_setup.shutil.which", lambda command: None)
    assert load_local_config(repo) == config
    assert local_path(repo).read_bytes() == before


def test_no_runtime_load_is_actionable_and_read_only(repo):
    with pytest.raises(ConfigError, match="machinist start"):
        load_local_config(repo)
    assert not (repo / ".machinist").exists()


@pytest.mark.parametrize(
    "relative",
    [
        ".machinist",
        ".machinist/runs",
        ".machinist/runs/local",
        ".machinist/runs/local/config.yaml",
    ],
)
def test_setup_and_load_reject_symlinked_runtime_paths(repo, tmp_path, relative):
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    if target.suffix:
        outside.write_text("version: 1\n")
    else:
        outside.mkdir()
    target.symlink_to(outside)
    with pytest.raises(ConfigError):
        ensure_local_config(repo, test_command="pytest")
    with pytest.raises(ConfigError):
        load_local_config(repo)


def test_runtime_config_reads_are_bounded(repo):
    ensure_local_config(repo, test_command="pytest")
    local_path(repo).write_text("#" * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ConfigError, match="[Ll]imit|maximum|bytes"):
        load_local_config(repo)


def test_repository_internal_workshop_root_is_rejected_before_write(repo):
    (repo / "machinist.yaml").write_text(f"workspace:\n  root: {repo / 'workshops'}\n")
    with pytest.raises(ConfigError, match="outside the repository"):
        ensure_local_config(repo, test_command="pytest")
    assert not local_path(repo).exists()


def test_relative_workshop_root_is_resolved_against_repository(
    repo, monkeypatch, tmp_path
):
    (repo / "machinist.yaml").write_text("workspace:\n  root: workshops\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ConfigError, match="outside the repository"):
        ensure_local_config(repo, test_command="pytest")


def test_external_relative_workshop_root_is_stable_from_subdirectory(repo, monkeypatch):
    (repo / "machinist.yaml").write_text("workspace:\n  root: ../workshops\n")
    expected = repo.parent / "workshops"
    ensure_local_config(repo, test_command="pytest")
    nested = repo / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert load_local_config(repo).workspace.resolved_root() == expected


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("review", {"enabled": False}, "review.enabled"),
        ("tests", {"command": None}, "required verification"),
        ("github", {"manage_workflows": True}, "manage_workflows"),
        ("telemetry", {"otlp_endpoint": "https://example.com"}, "otlp_endpoint"),
    ],
)
def test_modified_runtime_must_preserve_local_contract(repo, field, value, message):
    import yaml

    ensure_local_config(repo, test_command="pytest")
    payload = yaml.safe_load(local_path(repo).read_text())
    payload[field].update(value)
    local_path(repo).write_text(yaml.safe_dump(payload))
    before = local_path(repo).read_bytes()
    with pytest.raises(ConfigError, match=message):
        ensure_local_config(repo)
    assert local_path(repo).read_bytes() == before


def test_find_repository_root_accepts_subdirectories_without_origin(repo):
    nested = repo / "src/nested"
    nested.mkdir(parents=True)
    assert find_repository_root(nested) == repo


def test_find_repository_root_ignores_ambient_git_redirection(
    repo, tmp_path, monkeypatch
):
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    git(unrelated, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(unrelated))
    assert find_repository_root(repo) == repo


def test_find_repository_root_rejects_non_repository(tmp_path):
    with pytest.raises(ConfigError, match="git init"):
        find_repository_root(tmp_path)


@pytest.mark.parametrize(
    "files, expected",
    [
        (
            {"pyproject.toml": '[project]\ndependencies=["pytest>=8"]\n'},
            "python -m pytest",
        ),
        (
            {"pyproject.toml": '[dependency-groups]\ndev=["pytest"]\n', "uv.lock": ""},
            "uv run pytest",
        ),
        (
            {"pyproject.toml": "[tool.pytest.ini_options]\naddopts='-q'\n"},
            "python -m pytest",
        ),
        ({"package.json": json.dumps({"scripts": {"test": "vitest"}})}, "npm test"),
        (
            {"package.json": '{"scripts":{"test":"vitest"}}', "bun.lock": ""},
            "bun run test",
        ),
        (
            {"package.json": '{"scripts":{"test":"vitest"}}', "pnpm-lock.yaml": ""},
            "pnpm test",
        ),
        (
            {"package.json": '{"scripts":{"test":"vitest"}}', "yarn.lock": ""},
            "yarn test",
        ),
        ({"Cargo.toml": ""}, "cargo test"),
        ({"go.mod": ""}, "go test ./..."),
        ({"package.json": '{"scripts":{"test":"echo no test specified"}}'}, None),
        ({"pyproject.toml": "tool = 'bad shape'\n"}, None),
        ({"pyproject.toml": "bad[syntax", "package.json": "[]"}, None),
    ],
)
def test_test_command_detection_uses_real_test_configuration(tmp_path, files, expected):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    assert detect_test_command(tmp_path) == expected


def test_setup_uses_detected_test_command(repo):
    (repo / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    assert ensure_local_config(repo).tests.command == "npm test"


def test_advisory_only_config_requires_a_required_gate(repo):
    (repo / "machinist.yaml").write_text(
        "verification:\n  gates:\n    - name: lint\n      command: lint\n      required: false\n"
    )
    with pytest.raises(ConfigError, match="required"):
        ensure_local_config(repo)
    assert not local_path(repo).exists()


def test_explicit_installed_plugin_sets_all_phases(repo):
    config = ensure_local_config(
        repo, harness_name="local-plugin", test_command="pytest"
    )
    assert all(
        config.harness_for(phase).name == "local-plugin"
        for phase in ("spec", "execute", "review")
    )


def test_configured_custom_harness_command_is_preserved(repo, monkeypatch):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: local-plugin\n  command: /opt/bin/personal-agent\n"
    )
    monkeypatch.setattr(
        "machinist.local_setup.shutil.which",
        lambda command: command if command == "/opt/bin/personal-agent" else None,
    )
    config = ensure_local_config(repo, test_command="pytest")
    assert config.harness.command == "/opt/bin/personal-agent"


def test_configured_missing_adapter_reports_phase_before_writing(repo):
    (repo / "machinist.yaml").write_text("harness:\n  name: missing\n")
    with pytest.raises(ConfigError, match="configured Harness.*cannot run spec"):
        ensure_local_config(repo, test_command="pytest")
    assert not local_path(repo).exists()


def test_subsequent_start_checks_harness_availability_without_rewriting(
    repo, monkeypatch
):
    ensure_local_config(repo, test_command="pytest")
    before = local_path(repo).read_bytes()
    monkeypatch.setattr("machinist.local_setup.shutil.which", lambda command: None)
    with pytest.raises(ConfigError, match="PATH"):
        ensure_local_config(repo)
    assert local_path(repo).read_bytes() == before


def test_detected_required_gate_keeps_existing_advisory_gates(repo):
    (repo / "machinist.yaml").write_text(
        "verification:\n  gates:\n    - name: local-tests-1\n"
        "      command: lint\n      required: false\n"
    )
    (repo / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    config = ensure_local_config(repo)
    assert [(gate.name, gate.required) for gate in config.verification.gates] == [
        ("local-tests-1", False),
        ("local-tests-2", True),
    ]


def test_setup_refuses_tracked_runtime_records(repo):
    tracked = repo / ".machinist/runs/old.json"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("{}")
    git(repo, "add", str(tracked))
    with pytest.raises(ConfigError, match="must not be tracked"):
        ensure_local_config(repo, test_command="pytest")
    assert not local_path(repo).exists()


def test_matching_flags_preserve_harness_tuning_and_named_verification(repo):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: local-plugin\n  model: personal-model\n"
        "  timeout_minutes: 60\n  extra_args: [--verbose]\n"
        "verification:\n  gates:\n    - name: tests\n      command: pytest\n"
        "      timeout_minutes: 45\n    - name: types\n      command: mypy\n"
    )
    config = ensure_local_config(
        repo, harness_name="local-plugin", test_command="pytest"
    )
    assert config.harness.model == "personal-model"
    assert config.harness.timeout_minutes == 60
    assert config.harness.extra_args == ["--verbose"]
    assert [gate.name for gate in config.resolved_verification_gates()] == [
        "tests",
        "types",
    ]
    gate = config.resolved_verification_gates()[0]
    assert gate.timeout_minutes == 45
    assert gate.mutation_policy.value == "forbid"


def test_new_test_command_preserves_existing_named_gates(repo):
    (repo / "machinist.yaml").write_text(
        "verification:\n  gates:\n    - name: types\n      command: mypy\n"
    )
    config = ensure_local_config(repo, test_command="pytest")
    assert [gate.command for gate in config.resolved_verification_gates()] == [
        "mypy",
        "pytest",
    ]


def test_relative_harness_command_checks_repository_from_subdirectory(
    repo, monkeypatch
):
    executable = repo / "bin/local-agent"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    nested = repo / "src"
    nested.mkdir()
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: local-plugin\n  command: ./bin/local-agent\n"
    )
    monkeypatch.setattr(
        "machinist.local_setup.shutil.which",
        lambda command: command if Path(command).is_file() else None,
    )
    monkeypatch.chdir(nested)
    config = ensure_local_config(repo, test_command="pytest")
    assert config.harness.command == "./bin/local-agent"


def test_new_harness_preserves_timeouts_and_clears_old_provider_arguments(repo):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: codex\n  model: old-model\n  extra_args: [--verbose]\n"
        "  timeout_minutes: 60\n  spec_timeout_minutes: 20\n"
    )
    config = ensure_local_config(
        repo, harness_name="local-plugin", test_command="pytest"
    )
    assert config.harness.name == "local-plugin"
    assert config.harness.timeout_minutes == 60
    assert config.harness.spec_timeout_minutes == 20
    assert config.harness.model is None
    assert config.harness.extra_args == []


def test_new_base_harness_preserves_matching_phase_profile(repo):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: codex\n  model: old-model\n"
        "  execute:\n    name: local-plugin\n    model: selected-model\n"
        "    command: local-agent\n    timeout_minutes: 90\n"
        "  spec:\n    timeout_minutes: 25\n"
    )
    config = ensure_local_config(
        repo, harness_name="local-plugin", test_command="pytest"
    )
    assert config.harness_for("execute").model == "selected-model"
    assert config.harness_for("execute").command == "local-agent"
    assert config.harness_for("execute").timeout_minutes == 90
    assert config.harness_for("spec").spec_timeout_minutes == 25


def test_selected_harness_replaces_different_phase_provider_preserving_phase_timeout(
    repo,
):
    (repo / "machinist.yaml").write_text(
        "harness:\n  name: local-plugin\n  model: shared-model\n"
        "  execute:\n    name: codex\n    model: other-model\n"
        "    command: missing-codex\n    extra_args: [--verbose]\n    timeout_minutes: 45\n"
    )
    config = ensure_local_config(
        repo, harness_name="local-plugin", test_command="pytest"
    )
    phase = config.harness_for("execute")
    assert phase.name == "local-plugin"
    assert phase.model == "shared-model"
    assert phase.command is None
    assert phase.extra_args == []
    assert phase.timeout_minutes == 45
    assert ensure_local_config(repo, harness_name="local-plugin") == config

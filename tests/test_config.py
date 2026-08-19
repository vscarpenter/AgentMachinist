"""Tests for machinist.yaml schema, compatibility, and integration helpers."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from machinist.config import (
    MAX_INSTRUCTION_FILES_PER_PHASE,
    MAX_PHASE_INSTRUCTION_BYTES,
    AllowedHoursConfig,
    ConfigError,
    GateMutationPolicy,
    HarnessConfig,
    HarnessName,
    HarnessPhase,
    InstructionResolutionError,
    LimitsConfig,
    MachinistConfig,
    NotificationBackend,
    SpecInstall,
    config_json_schema,
    load_config,
    reserved_harness_extra_args,
)

FULL_YAML = """\
version: 1
harness:
  name: codex
  command: /opt/bin/codex
  timeout_minutes: 45
  spec_timeout_minutes: 5
  spec:
    name: claude-code
    command: /opt/bin/claude
    model: claude-fast
    timeout_minutes: 8
  execute:
    model: codex-strong
    timeout_minutes: 60
github:
  repo: vscarpenter/demo
  spec_source: github-actions
  spec_install: pypi
  manage_workflows: false
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
    assert config.harness_for("spec").name is HarnessName.CLAUDE_CODE
    assert config.harness_for("spec").command == "/opt/bin/claude"
    assert config.harness_for("spec").spec_timeout_minutes == 8
    assert config.harness_for("execute").name is HarnessName.CODEX
    assert config.harness_for("execute").model == "codex-strong"
    assert config.harness_for("execute").timeout_minutes == 60
    assert config.github.repo == "vscarpenter/demo"
    assert config.github.spec_source.value == "github-actions"
    assert config.github.manage_workflows is False
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
    assert config.github.manage_workflows is True


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


@pytest.mark.parametrize("value", [0, 2, True, 1.0, "1", "2026-08-19"])
def test_only_config_version_one_is_supported(tmp_path, value):
    path = write_config(tmp_path, f"version: {value!r}\n")
    with pytest.raises(ConfigError, match="version"):
        load_config(path)


def test_legacy_harness_shape_resolves_for_both_phases():
    config = MachinistConfig.model_validate(
        {
            "harness": {
                "name": "codex",
                "command": "/opt/codex",
                "model": "shared",
                "extra_args": ["--verbose"],
                "timeout_minutes": 40,
                "spec_timeout_minutes": 7,
            }
        }
    )

    spec = config.harness_for(HarnessPhase.SPEC)
    execute = config.harness_for(HarnessPhase.EXECUTE)

    assert config.harness.name is HarnessName.CODEX  # old API remains intact
    assert spec.name is execute.name is HarnessName.CODEX
    assert spec.command == execute.command == "/opt/codex"
    assert spec.model == execute.model == "shared"
    assert spec.spec_timeout_minutes == 7
    assert execute.timeout_minutes == 40


def test_phase_harness_overrides_inherit_and_can_clear_legacy_values():
    config = MachinistConfig.model_validate(
        {
            "harness": {
                "name": "claude-code",
                "command": "/opt/claude",
                "model": "shared",
                "extra_args": ["--verbose"],
                "spec": {
                    "name": "codex",
                    "command": None,
                    "model": "spec-model",
                    "extra_args": [],
                    "timeout_minutes": 4,
                },
                "execute": {"timeout_minutes": 90},
            }
        }
    )

    spec = config.harness_for("spec")
    execute = config.harness_for("execute")
    assert spec.name is HarnessName.CODEX
    assert spec.command is None
    assert spec.model == "spec-model"
    assert spec.extra_args == []
    assert spec.spec_timeout_minutes == 4
    assert execute.name is HarnessName.CLAUDE_CODE
    assert execute.command == "/opt/claude"
    assert execute.model == "shared"
    assert execute.extra_args == ["--verbose"]
    assert execute.timeout_minutes == 90


def test_null_phase_timeout_inherits_legacy_budget():
    config = MachinistConfig.model_validate(
        {
            "harness": {
                "timeout_minutes": 41,
                "spec_timeout_minutes": 6,
                "spec": {"timeout_minutes": None},
                "execute": {"timeout_minutes": None},
            }
        }
    )
    assert config.harness_for("spec").spec_timeout_minutes == 6
    assert config.harness_for("execute").timeout_minutes == 41


def test_instruction_overlays_default_to_empty_without_filesystem_access(tmp_path):
    config = MachinistConfig()

    assert config.instructions.spec.paths == []
    assert config.instructions.execute.append is None
    assert config.resolve_instructions("spec", tmp_path / "does-not-exist") == ""


def test_instruction_overlay_resolves_ordered_files_then_inline_text(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Shared repository rules")
    (repo / "docs" / "spec.md").write_text("Spec-only guidance")
    config = MachinistConfig.model_validate(
        {
            "instructions": {
                "spec": {
                    "paths": ["AGENTS.md", "docs/spec.md"],
                    "append": "Keep the plan narrow.",
                },
                "execute": {"append": "Preserve public APIs."},
            }
        }
    )

    assert config.resolve_instructions("spec", repo) == (
        "Shared repository rules\n\nSpec-only guidance\n\nKeep the plan narrow."
    )
    assert config.resolve_instructions("execute", repo) == "Preserve public APIs."


def test_instruction_paths_are_not_read_during_model_validation(tmp_path):
    config = MachinistConfig.model_validate(
        {"instructions": {"spec": {"paths": ["missing.md"]}}}
    )

    assert config.instructions.spec.paths == ["missing.md"]
    with pytest.raises(InstructionResolutionError, match="missing.md"):
        config.resolve_instructions("spec", tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "./instructions.md",
        "../outside.md",
        "/absolute.md",
        "docs//instructions.md",
        "docs/instructions.md/",
        "docs\\instructions.md",
        "~/instructions.md",
        "bad\ninstructions.md",
    ],
)
def test_instruction_paths_must_be_canonical_repo_relative_posix_paths(path):
    with pytest.raises(ValueError, match="instruction paths"):
        MachinistConfig.model_validate({"instructions": {"spec": {"paths": [path]}}})


def test_instruction_paths_reject_duplicates_and_excessive_file_counts():
    with pytest.raises(ValueError, match="duplicates"):
        MachinistConfig.model_validate(
            {"instructions": {"spec": {"paths": ["AGENTS.md", "AGENTS.md"]}}}
        )
    with pytest.raises(ValueError):
        MachinistConfig.model_validate(
            {
                "instructions": {
                    "execute": {
                        "paths": [
                            f"docs/instruction-{index}.md"
                            for index in range(MAX_INSTRUCTION_FILES_PER_PHASE + 1)
                        ]
                    }
                }
            }
        )


@pytest.mark.parametrize("append", ["", "   ", "bad\x00text"])
def test_inline_instruction_append_must_be_usable(append):
    with pytest.raises(ValueError, match="instructions append"):
        MachinistConfig.model_validate({"instructions": {"spec": {"append": append}}})


def test_inline_instruction_append_has_a_utf8_byte_limit():
    with pytest.raises(ValueError, match="100000 UTF-8 bytes"):
        MachinistConfig.model_validate(
            {
                "instructions": {
                    "spec": {"append": "é" * (MAX_PHASE_INSTRUCTION_BYTES // 2 + 1)}
                }
            }
        )


def test_instruction_resolver_rejects_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (repo / "linked.md").symlink_to(outside)
    config = MachinistConfig.model_validate(
        {"instructions": {"execute": {"paths": ["linked.md"]}}}
    )

    with pytest.raises(InstructionResolutionError, match="escapes repository root"):
        config.resolve_instructions("execute", repo)


def test_instruction_resolver_accepts_contained_symlink_and_rejects_aliases(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "rules.md").write_text("rules")
    (repo / "linked.md").symlink_to("rules.md")
    contained = MachinistConfig.model_validate(
        {"instructions": {"spec": {"paths": ["linked.md"]}}}
    )
    duplicate = MachinistConfig.model_validate(
        {"instructions": {"spec": {"paths": ["rules.md", "linked.md"]}}}
    )

    assert contained.resolve_instructions("spec", repo) == "rules"
    with pytest.raises(InstructionResolutionError, match="same file"):
        duplicate.resolve_instructions("spec", repo)


def test_instruction_resolver_requires_regular_utf8_nul_free_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "directory").mkdir()
    (repo / "binary.md").write_bytes(b"\xff\xfe")
    (repo / "nul.md").write_bytes(b"before\x00after")

    for path, message in (
        ("directory", "regular file"),
        ("binary.md", "valid UTF-8"),
        ("nul.md", "NUL byte"),
    ):
        config = MachinistConfig.model_validate(
            {"instructions": {"execute": {"paths": [path]}}}
        )
        with pytest.raises(InstructionResolutionError, match=message):
            config.resolve_instructions("execute", repo)


def test_instruction_resolver_enforces_file_and_aggregate_size_limit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "too-large.md").write_bytes(b"x" * (MAX_PHASE_INSTRUCTION_BYTES + 1))
    too_large = MachinistConfig.model_validate(
        {"instructions": {"spec": {"paths": ["too-large.md"]}}}
    )
    with pytest.raises(InstructionResolutionError, match="exceeds"):
        too_large.resolve_instructions("spec", repo)

    (repo / "one.md").write_bytes(b"a" * 60_000)
    (repo / "two.md").write_bytes(b"b" * 40_000)
    aggregate = MachinistConfig.model_validate(
        {"instructions": {"spec": {"paths": ["one.md", "two.md"]}}}
    )
    with pytest.raises(InstructionResolutionError, match="maximum"):
        aggregate.resolve_instructions("spec", repo)


def test_spec_profile_keeps_the_legacy_sixty_minute_ceiling():
    with pytest.raises(ValueError, match="cannot exceed 60"):
        HarnessConfig.model_validate({"spec": {"timeout_minutes": 61}})


def test_changing_phase_provider_does_not_inherit_provider_specific_options():
    config = MachinistConfig.model_validate(
        {
            "harness": {
                "name": "codex",
                "command": "/opt/custom-codex",
                "model": "codex-model",
                "extra_args": ["--verbose"],
                "spec": {"name": "claude-code"},
            }
        }
    )
    spec = config.harness_for("spec")
    assert spec.name is HarnessName.CLAUDE_CODE
    assert spec.command is None
    assert spec.model is None
    assert spec.extra_args == []


@pytest.mark.parametrize(
    ("name", "argument"),
    [
        ("claude-code", "--permission-mode=bypassPermissions"),
        ("codex", "--sandbox=danger-full-access"),
        ("codex", "-C/tmp/outside"),
        ("codex", "--config=sandbox_mode=danger-full-access"),
        ("pi", "--tools"),
        ("opencode", "--agent"),
    ],
)
def test_legacy_extra_args_cannot_override_adapter_safety(name, argument):
    with pytest.raises(ValueError, match="adapter-owned"):
        HarnessConfig(name=name, extra_args=[argument])


def test_phase_extra_args_are_validated_against_resolved_provider():
    with pytest.raises(ValueError, match="codex spec"):
        HarnessConfig.model_validate(
            {
                "spec": {
                    "name": "codex",
                    "extra_args": ["--dangerously-bypass-approvals-and-sandbox"],
                }
            }
        )


def test_reserved_argument_map_is_exposed_for_adapter_contract_tests():
    assert "--sandbox" in reserved_harness_extra_args(HarnessName.CODEX, "spec")
    assert "--permission-mode" in reserved_harness_extra_args(
        HarnessName.CLAUDE_CODE, "execute"
    )


def test_github_actions_rejects_non_claude_spec_provider(tmp_path):
    path = write_config(
        tmp_path,
        "harness:\n  name: codex\ngithub:\n  spec_source: github-actions\n",
    )
    with pytest.raises(ConfigError, match="only.*claude-code"):
        load_config(path)


def test_github_actions_accepts_claude_spec_profile_over_non_claude_default():
    config = MachinistConfig.model_validate(
        {
            "harness": {"name": "codex", "spec": {"name": "claude-code"}},
            "github": {"spec_source": "github-actions"},
        }
    )
    assert config.harness_for("spec").name is HarnessName.CLAUDE_CODE
    assert config.harness_for("execute").name is HarnessName.CODEX


def test_unmanaged_github_actions_can_use_an_external_provider_workflow():
    config = MachinistConfig.model_validate(
        {
            "harness": {"name": "codex"},
            "github": {
                "spec_source": "github-actions",
                "manage_workflows": False,
            },
        }
    )
    assert config.github.manage_workflows is False
    assert config.harness_for("spec").name is HarnessName.CODEX


def test_legacy_test_command_normalizes_to_required_mutating_gate():
    config = MachinistConfig.model_validate(
        {"harness": {"timeout_minutes": 47}, "tests": {"command": "pytest -q"}}
    )

    gates = config.resolved_verification_gates()

    assert len(gates) == 1
    assert gates[0].name == "legacy-tests"
    assert gates[0].command == "pytest -q"
    assert gates[0].timeout_minutes == 47
    assert gates[0].required is True
    assert gates[0].mutation_policy is GateMutationPolicy.ALLOW
    assert config.tests.command == "pytest -q"  # old consumer API remains intact


def test_named_verification_gates_preserve_order_and_policy():
    config = MachinistConfig.model_validate(
        {
            "verification": {
                "gates": [
                    {
                        "name": "lint",
                        "command": "ruff check .",
                        "timeout_minutes": 5,
                        "required": False,
                    },
                    {
                        "name": "tests",
                        "command": "pytest -q",
                        "timeout_minutes": 20,
                        "required": True,
                        "mutation_policy": "allow",
                    },
                ]
            }
        }
    )

    gates = config.resolved_verification_gates()
    assert [gate.name for gate in gates] == ["lint", "tests"]
    assert gates[0].required is False
    assert gates[0].mutation_policy is GateMutationPolicy.FORBID
    assert gates[1].required is True
    assert gates[1].mutation_policy is GateMutationPolicy.ALLOW


def test_named_and_legacy_gates_are_ambiguous_and_rejected(tmp_path):
    path = write_config(
        tmp_path,
        "tests:\n  command: pytest\nverification:\n  gates:\n"
        "    - {name: lint, command: ruff}\n",
    )
    with pytest.raises(ConfigError, match="either legacy tests.command"):
        load_config(path)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "verification:\n  gates:\n    - {name: lint, command: ''}\n",
        "verification:\n  gates:\n    - {name: lint, command: ruff}\n"
        "    - {name: LINT, command: ruff}\n",
        "verification:\n  gates:\n    - {name: lint, command: ruff, timeout_minutes: 0}\n",
        "verification:\n  gates:\n"
        "    - {name: format, command: fmt, required: false, mutation_policy: allow}\n",
    ],
)
def test_invalid_named_gate_contracts_fail_closed(tmp_path, yaml_text):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, yaml_text))


def test_queue_defaults_to_one_task_per_pass_and_accepts_budgets():
    default = MachinistConfig()
    configured = MachinistConfig.model_validate(
        {
            "queue": {
                "max_tasks_per_pass": 3,
                "allowed_hours": {
                    "start": "09:00",
                    "end": "17:00",
                    "timezone": "America/Chicago",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                },
                "task_budget": {
                    "max_tasks_per_day": 8,
                    "max_runtime_minutes_per_day": 240,
                    "timezone": "America/Chicago",
                },
            }
        }
    )
    assert default.queue.max_tasks_per_pass == 1
    assert configured.queue.max_tasks_per_pass == 3
    assert configured.queue.task_budget.max_tasks_per_day == 8
    assert configured.queue.task_budget.timezone == "America/Chicago"


def test_allowed_hours_handles_daytime_and_overnight_windows():
    daytime = AllowedHoursConfig(
        start="09:00", end="17:00", timezone="UTC", days=["mon"]
    )
    overnight = AllowedHoursConfig(
        start="22:00", end="02:00", timezone="UTC", days=["mon"]
    )
    assert daytime.contains(datetime(2026, 8, 17, 12, tzinfo=UTC))
    assert not daytime.contains(datetime(2026, 8, 17, 18, tzinfo=UTC))
    assert overnight.contains(datetime(2026, 8, 17, 23, tzinfo=UTC))
    assert overnight.contains(datetime(2026, 8, 18, 1, tzinfo=UTC))
    assert not overnight.contains(datetime(2026, 8, 18, 3, tzinfo=UTC))


@pytest.mark.parametrize(
    "yaml_text",
    [
        "queue:\n  max_tasks_per_pass: 0\n",
        "queue:\n  allowed_hours: {start: '9:00', end: '17:00'}\n",
        "queue:\n  allowed_hours: {start: '09:00', end: '09:00'}\n",
        "queue:\n  task_budget: {}\n",
        "queue:\n  task_budget: {max_tasks_per_day: 1, timezone: Mars/Olympus}\n",
    ],
)
def test_invalid_queue_limits_fail_closed(tmp_path, yaml_text):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, yaml_text))


def test_notifications_keep_legacy_failure_desktop_default():
    config = MachinistConfig()
    assert config.notifications.backend is NotificationBackend.DESKTOP
    assert [event.value for event in config.notifications.events] == ["failure"]


def test_command_and_webhook_notification_shapes_keep_shell_and_secrets_out():
    command = MachinistConfig.model_validate(
        {
            "notifications": {
                "backend": "command",
                "events": ["failure", "pr_ready"],
                "command": {"argv": ["/usr/local/bin/notifier", "--json"]},
            }
        }
    )
    webhook = MachinistConfig.model_validate(
        {
            "notifications": {
                "backend": "webhook",
                "webhook": {
                    "url_env": "MY_WEBHOOK_URL",
                    "authorization_env": "MY_WEBHOOK_AUTH",
                },
            }
        }
    )
    assert command.notifications.command.argv[0] == "/usr/local/bin/notifier"
    assert webhook.notifications.webhook.url_env == "MY_WEBHOOK_URL"


@pytest.mark.parametrize(
    "notification",
    [
        {"backend": "command"},
        {"backend": "webhook"},
        {"backend": "desktop", "command": {"argv": ["notify"]}},
        {"backend": "webhook", "webhook": {"url_env": "not valid"}},
    ],
)
def test_invalid_notification_backend_shapes_fail_closed(notification):
    with pytest.raises(ValueError):
        MachinistConfig.model_validate({"notifications": notification})


def test_limits_have_conservative_defaults_and_denied_path_helper():
    limits = LimitsConfig()
    assert limits.max_issue_body_chars == 50_000
    assert limits.max_spec_chars == 100_000
    assert limits.max_changed_files == 100
    assert limits.max_changed_bytes == 5 * 1024 * 1024
    assert limits.allow_binary is False
    assert limits.path_is_denied(".machinist/specs/issue-1-spec.md")
    assert not limits.path_is_denied("src/machinist/config.py")
    assert limits.path_is_denied("../outside")  # invalid input fails closed


@pytest.mark.parametrize(
    "denied_path", ["../outside", "/absolute", "bad\\windows", ".", ""]
)
def test_denied_paths_must_be_repository_relative(tmp_path, denied_path):
    path = write_config(tmp_path, f"limits:\n  denied_paths: [{denied_path!r}]\n")
    with pytest.raises(ConfigError, match="denied paths"):
        load_config(path)


def test_json_schema_is_generated_from_the_runtime_model():
    schema = config_json_schema()
    assert schema["properties"]["version"]["const"] == 1
    for section in (
        "harness",
        "instructions",
        "github",
        "verification",
        "queue",
        "notifications",
        "limits",
    ):
        assert section in schema["properties"]

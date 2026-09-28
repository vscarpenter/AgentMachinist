"""Tests for the harness abstraction layer."""

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from machinist.config import HarnessConfig, HarnessName
from machinist.harness import get_harness
from machinist.harness.base import Harness, HarnessError
from machinist.process import ProcessSignalInterruption, ProcessStragglerError


class FakeRunner:
    def __init__(self, *results):
        self.calls = []
        self._results = list(results)

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        stdout, returncode, stderr = result
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class PythonProbeHarness(Harness):
    name = "python-probe"
    default_command = sys.executable

    def spec_argv(self, prompt):
        return [
            self.command,
            "-c",
            f"import time; time.sleep(0.2); print({prompt!r})",
        ]

    def implement_argv(self, prompt):
        return self.spec_argv(prompt)


@pytest.mark.parametrize("name", list(HarnessName))
def test_registry_resolves_every_configured_harness(name):
    harness = get_harness(HarnessConfig(name=name))
    assert harness.name == name.value


@pytest.mark.parametrize("name", list(HarnessName))
def test_prompt_appears_in_every_adapters_argv(name):
    harness = get_harness(HarnessConfig(name=name))
    assert "do the thing" in harness.spec_argv("do the thing")
    assert "do the thing" in harness.implement_argv("do the thing")


def test_config_command_overrides_default_executable():
    config = HarnessConfig(name=HarnessName.CLAUDE_CODE, command="/opt/claude-beta")
    harness = get_harness(config)
    assert harness.spec_argv("hi")[0] == "/opt/claude-beta"


def test_claude_code_spec_argv_is_headless_print_mode():
    harness = get_harness(HarnessConfig(name=HarnessName.CLAUDE_CODE))
    argv = harness.spec_argv("write a spec")
    assert argv[:3] == ["claude", "-p", "write a spec"]
    assert ["--permission-mode", "plan"] == argv[argv.index("--permission-mode") :][:2]
    assert ["--tools", "Read,Grep,Glob"] == argv[argv.index("--tools") :][:2]
    assert "--no-session-persistence" in argv


def test_spec_argv_is_read_only_for_every_harness():
    # Phase 1 must not be able to edit files: stray edits would be swept
    # into the spec commit. Flags verified against the real CLIs 2026-08-16.
    expectations = {
        HarnessName.CLAUDE_CODE: [
            "--permission-mode",
            "plan",
            "--tools",
            "Read,Grep,Glob",
        ],
        HarnessName.OPENCODE: ["--agent", "plan", "--pure"],
        HarnessName.PI: [
            "--tools",
            "read,grep,find,ls",
            "--no-extensions",
            "--no-session",
        ],
        HarnessName.CODEX: ["--sandbox", "read-only"],
    }
    for name, flags in expectations.items():
        argv = get_harness(HarnessConfig(name=name)).spec_argv("p")
        for flag in flags:
            assert flag in argv, f"{name.value} spec argv missing {flag}: {argv}"


def test_claude_code_implement_argv_can_edit_files():
    harness = get_harness(HarnessConfig(name=HarnessName.CLAUDE_CODE))
    argv = harness.implement_argv("build it")
    assert argv[:3] == ["claude", "-p", "build it"]
    assert "--permission-mode" in argv
    assert "--no-session-persistence" in argv
    # Without allowed commands there must be no Bash allowlist at all.
    assert "--allowedTools" not in argv


def test_claude_code_implement_argv_allowlists_exact_gate_commands():
    # The verification feedback loop grants exactly the configured gate
    # commands (exact and prefix forms), nothing broader.
    harness = get_harness(HarnessConfig(name=HarnessName.CLAUDE_CODE))
    harness.allowed_commands = ("uv run pytest", "make lint")
    argv = harness.implement_argv("build it")
    index = argv.index("--allowedTools")
    assert argv[index + 1 : index + 5] == [
        "Bash(uv run pytest)",
        "Bash(uv run pytest:*)",
        "Bash(make lint)",
        "Bash(make lint:*)",
    ]


def test_allowed_commands_never_reach_spec_argv():
    harness = get_harness(HarnessConfig(name=HarnessName.CLAUDE_CODE))
    harness.allowed_commands = ("uv run pytest",)
    assert "--allowedTools" not in harness.spec_argv("write a spec")


def test_allowed_commands_leave_other_implement_argvs_unchanged():
    # codex workspace-write, opencode run, pi -p, and goose run already permit
    # command execution in their execute modes; the allowlist is claude-code-only.
    for name in (
        HarnessName.CODEX,
        HarnessName.OPENCODE,
        HarnessName.PI,
        HarnessName.GOOSE,
    ):
        harness = get_harness(HarnessConfig(name=name))
        baseline = harness.implement_argv("p")
        harness.allowed_commands = ("uv run pytest",)
        assert harness.implement_argv("p") == baseline, name.value


def test_codex_implement_argv_uses_current_headless_workspace_write_contract():
    argv = get_harness(HarnessConfig(name=HarnessName.CODEX)).implement_argv("build")

    assert argv == [
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "-c",
        'approval_policy="never"',
        "--ephemeral",
        "build",
    ]
    assert "--full-auto" not in argv


def test_goose_argv_pins_headless_profiles_and_splices_passthrough_before_prompt():
    # Flags verified against `goose run --help` on Goose 1.52.0, 2026-09-27.
    harness = get_harness(
        HarnessConfig(
            name=HarnessName.GOOSE,
            model="claude-sonnet-4",
            extra_args=["--provider", "anthropic"],
        )
    )
    read_only = [
        "goose",
        "run",
        "-q",
        "--no-session",
        "--no-profile",
        "--with-builtin",
        "developer",
        "--output-format",
        "json",
        "--model",
        "claude-sonnet-4",
        "--provider",
        "anthropic",
        "--text",
        "write a spec",
    ]

    assert harness.spec_argv("write a spec") == read_only
    assert harness.review_argv("write a spec") == read_only
    assert harness.implement_argv("build it") == [
        "goose",
        "run",
        "--no-session",
        "--model",
        "claude-sonnet-4",
        "--provider",
        "anthropic",
        "--text",
        "build it",
    ]


# Real `goose run --output-format json` output from Goose 1.52.0 (2026-09-28).
# The run used the shell tool, whose output `hello {name!r}` once broke parsing.
_GOOSE_RUN = Path(__file__).parent / "fixtures" / "goose-1.52-run.json"


@pytest.mark.parametrize("method", ["generate_spec", "review"])
def test_goose_spec_and_review_return_only_the_final_answer(tmp_path, method):
    runner = FakeRunner((_GOOSE_RUN.read_text(), 0, ""))
    harness = get_harness(HarnessConfig(name=HarnessName.GOOSE), runner=runner)

    assert getattr(harness, method)("p", cwd=tmp_path) == "ok"


@pytest.mark.parametrize(
    "stdout",
    [
        "  \u25b8 shell\nhello",
        '{"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}',
        # A run that ends on a tool call must not fall back to earlier narration.
        '{"messages": ['
        '{"role": "assistant", "content": [{"type": "text", "text": "Let me look."}]},'
        '{"role": "assistant", "content": [{"type": "toolRequest", "id": "t"}]}]}',
    ],
)
def test_goose_output_without_a_final_answer_fails_loudly(tmp_path, stdout):
    runner = FakeRunner((stdout, 0, ""))
    harness = get_harness(HarnessConfig(name=HarnessName.GOOSE), runner=runner)

    with pytest.raises(HarnessError, match="goose"):
        harness.generate_spec("p", cwd=tmp_path)


def test_goose_execute_keeps_its_full_text_transcript(tmp_path):
    runner = FakeRunner(("  \u25b8 shell\nedited files\n", 0, ""))
    harness = get_harness(HarnessConfig(name=HarnessName.GOOSE), runner=runner)

    assert harness.implement("p", cwd=tmp_path) == "  \u25b8 shell\nedited files\n"


def test_goose_runs_pin_autonomous_mode_over_operator_setting(tmp_path, monkeypatch):
    # approve and smart_approve wait for a confirmation no headless run can give.
    monkeypatch.setenv("GOOSE_MODE", "approve")
    runner = FakeRunner((_GOOSE_RUN.read_text(), 0, ""), ("done", 0, ""))
    harness = get_harness(HarnessConfig(name=HarnessName.GOOSE), runner=runner)

    harness.generate_spec("p", cwd=tmp_path)
    harness.implement("p", cwd=tmp_path)

    assert [call[1]["env"]["GOOSE_MODE"] for call in runner.calls] == ["auto"] * 2


def test_goose_is_advisory_explicit_only_and_has_no_hosted_ci_or_auth_probe():
    harness = get_harness(HarnessConfig(name=HarnessName.GOOSE))

    assert harness.capabilities.spec_repository_writes == "advisory"
    assert harness.descriptor.phases == frozenset({"spec", "execute", "review"})
    assert harness.descriptor.documentation_url == "https://goose-docs.ai/docs/"
    assert harness.descriptor.ci_spec is None
    assert harness.authentication_argv() is None
    # pressly/goose, a Go migration tool, installs the same executable name.
    assert type(harness).auto_select is False


def test_other_builtin_adapters_pin_no_environment_and_stay_auto_selectable():
    for name in HarnessName:
        if name is HarnessName.GOOSE:
            continue
        harness = get_harness(HarnessConfig(name=name))
        assert harness.environment_overrides() == {}, name.value
        assert type(harness).auto_select is True, name.value


def test_authentication_probes_fail_closed_on_empty_or_unstructured_output():
    claude = get_harness(HarnessConfig(name=HarnessName.CLAUDE_CODE))
    assert claude.authentication_argv() == ["claude", "auth", "status", "--json"]
    assert not claude.authentication_ready(
        subprocess.CompletedProcess([], 0, "logged in", "")
    )

    opencode = get_harness(HarnessConfig(name=HarnessName.OPENCODE))
    assert opencode.authentication_argv() == ["opencode", "auth", "list", "--pure"]
    assert not opencode.authentication_ready(
        subprocess.CompletedProcess([], 0, "0 credentials", "")
    )
    assert opencode.authentication_ready(
        subprocess.CompletedProcess([], 0, "2 credentials", "")
    )


def test_pi_authentication_probe_uses_configured_model_and_structured_status():
    harness = get_harness(
        HarnessConfig(name=HarnessName.PI, model="anthropic/claude-sonnet")
    )

    assert harness.authentication_argv() == [
        "pi",
        "auth",
        "check",
        "--model",
        "anthropic/claude-sonnet",
        "--json",
        "--no-refresh",
    ]
    assert harness.authentication_ready(
        subprocess.CompletedProcess([], 0, '{"status":"ready"}', "")
    )
    assert not harness.authentication_ready(
        subprocess.CompletedProcess([], 1, '{"status":"not_ready"}', "")
    )
    assert "--no-session" in harness.implement_argv("build")


def test_generate_spec_runs_in_cwd_with_spec_timeout(tmp_path):
    runner = FakeRunner(("the spec text", 0, ""))
    config = HarnessConfig(spec_timeout_minutes=5, timeout_minutes=45)
    harness = get_harness(config, runner=runner)

    output = harness.generate_spec("write a spec", cwd=tmp_path)

    assert output == "the spec text"
    _args, kwargs = runner.calls[0]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 5 * 60


def test_implement_uses_the_larger_timeout(tmp_path):
    runner = FakeRunner(("done", 0, ""))
    config = HarnessConfig(spec_timeout_minutes=5, timeout_minutes=45)
    harness = get_harness(config, runner=runner)

    harness.implement("build it", cwd=tmp_path)

    _, kwargs = runner.calls[0]
    assert kwargs["timeout"] == 45 * 60


def test_nonzero_exit_raises_harness_error_with_stderr(tmp_path):
    runner = FakeRunner(("", 2, "rate limited"))
    harness = get_harness(HarnessConfig(), runner=runner)

    with pytest.raises(HarnessError, match="rate limited"):
        harness.generate_spec("write a spec", cwd=tmp_path)


def test_nonzero_exit_bounds_harness_diagnostic_to_a_useful_tail(tmp_path):
    runner = FakeRunner(("", 2, "prefix" + "x" * 10_000 + "useful-tail"))
    harness = get_harness(HarnessConfig(), runner=runner)

    with pytest.raises(HarnessError) as caught:
        harness.generate_spec("write a spec", cwd=tmp_path)

    message = str(caught.value)
    assert "useful-tail" in message
    assert "prefix" not in message
    assert len(message) < 5_000


def test_progress_callback_fires_during_long_runs(tmp_path):
    def slow_runner(args, **kwargs):
        time.sleep(0.3)
        return subprocess.CompletedProcess(args, 0, "done", "")

    harness = get_harness(HarnessConfig(), runner=slow_runner)
    harness.heartbeat_seconds = 0.05
    beats = []
    harness.on_progress = beats.append

    assert harness.generate_spec("p", cwd=tmp_path) == "done"
    assert beats
    assert "claude-code" in beats[0]
    assert "elapsed" in beats[0]


def test_default_supervisor_preserves_output_and_heartbeat_contract(tmp_path):
    harness = PythonProbeHarness(HarnessConfig())
    harness.heartbeat_seconds = 0.05
    beats = []
    harness.on_progress = beats.append

    assert (
        harness.generate_spec("the generated spec", cwd=tmp_path)
        == "the generated spec\n"
    )
    assert beats
    assert all("python-probe still working" in beat for beat in beats)


def test_default_supervisor_preserves_missing_executable_error_contract(tmp_path):
    missing = tmp_path / "missing-harness"
    harness = PythonProbeHarness(HarnessConfig(command=str(missing)))

    with pytest.raises(HarnessError, match="harness executable.*not found"):
        harness.generate_spec("p", cwd=tmp_path)


def test_no_progress_callback_is_fine(tmp_path):
    runner = FakeRunner(("ok", 0, ""))
    harness = get_harness(HarnessConfig(), runner=runner)

    assert harness.generate_spec("p", cwd=tmp_path) == "ok"


def test_errors_still_surface_with_progress_enabled(tmp_path):
    runner = FakeRunner(("", 3, "kaboom"))
    harness = get_harness(HarnessConfig(), runner=runner)
    harness.on_progress = lambda msg: None

    with pytest.raises(HarnessError, match="kaboom"):
        harness.generate_spec("p", cwd=tmp_path)


def test_timeout_raises_harness_error(tmp_path):
    runner = FakeRunner(subprocess.TimeoutExpired(cmd=["claude"], timeout=600))
    harness = get_harness(HarnessConfig(), runner=runner)

    with pytest.raises(HarnessError, match="timed out"):
        harness.generate_spec("write a spec", cwd=tmp_path)


def test_background_process_straggler_raises_stable_harness_error(tmp_path):
    runner = FakeRunner(
        ProcessStragglerError(
            ["claude"],
            0,
            stdout="generated output",
            stderr="background helper remained",
        )
    )
    harness = get_harness(HarnessConfig(), runner=runner)

    with pytest.raises(
        HarnessError,
        match="left background processes running after exit; they were terminated",
    ):
        harness.generate_spec("write a spec", cwd=tmp_path)


def test_service_signal_interruption_is_not_downgraded_to_task_failure(tmp_path):
    def interrupted_runner(command, **_kwargs):
        raise ProcessSignalInterruption(command, signal.SIGTERM)

    harness = get_harness(HarnessConfig(), runner=interrupted_runner)

    with pytest.raises(ProcessSignalInterruption) as caught:
        harness.generate_spec("write a spec", cwd=tmp_path)

    assert caught.value.code == 128 + signal.SIGTERM
    assert caught.value.cancelled is True


def test_harness_subprocess_strips_controller_credentials_but_keeps_provider_key(
    tmp_path, monkeypatch
):
    runner = FakeRunner(("ok", 0, ""))
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "azure-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/google.json")
    monkeypatch.setenv("SOME_CLOUD_TOKEN", "cloud-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret")
    harness = get_harness(HarnessConfig(), runner=runner)

    harness.generate_spec("p", cwd=tmp_path)

    env = runner.calls[0][1]["env"]
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "AZURE_CLIENT_SECRET" not in env
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "SOME_CLOUD_TOKEN" not in env
    assert env["ANTHROPIC_API_KEY"] == "provider-secret"
    assert env["PATH"]
    assert env["HOME"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


class ModePinningHarness(Harness):
    name = "mode-pinning"
    default_command = "mode-cli"

    def environment_overrides(self):
        return {"PROBE_MODE": "auto"}

    def spec_argv(self, prompt):
        return [self.command, prompt]

    def implement_argv(self, prompt):
        return [self.command, prompt]


def test_environment_overrides_replace_inherited_values_in_every_phase(
    tmp_path, monkeypatch
):
    # An adapter whose CLI reads its permission mode from the environment
    # must win over the operator's shell for Spec, Execute, and Review alike.
    monkeypatch.setenv("PROBE_MODE", "approve")
    runner = FakeRunner(("spec", 0, ""), ("done", 0, ""), ("{}", 0, ""))
    harness = ModePinningHarness(HarnessConfig(), runner=runner)

    harness.generate_spec("p", cwd=tmp_path)
    harness.implement("p", cwd=tmp_path)
    harness.review("p", cwd=tmp_path)

    assert [call[1]["env"]["PROBE_MODE"] for call in runner.calls] == ["auto"] * 3


def test_harness_base_defaults_add_no_environment_and_allow_auto_selection():
    harness = PythonProbeHarness(HarnessConfig())
    assert harness.environment_overrides() == {}
    assert PythonProbeHarness.auto_select is True


def test_adapters_publish_honest_policy_capabilities():
    for name in HarnessName:
        capability = get_harness(HarnessConfig(name=name)).capabilities
        assert capability.spec_repository_writes in {"cli-enforced", "advisory"}
        assert capability.implementation_git_control == "prompt-and-postcondition"


def test_harness_model_and_extra_args_in_argv():
    for name in HarnessName:
        config = HarnessConfig(
            name=name, model="custom-model", extra_args=["--verbose", "--flag"]
        )
        harness = get_harness(config)
        spec_argv = harness.spec_argv("prompt")
        impl_argv = harness.implement_argv("prompt")
        assert "--model" in spec_argv and "custom-model" in spec_argv
        assert "--model" in impl_argv and "custom-model" in impl_argv
        assert "--verbose" in spec_argv and "--flag" in spec_argv
        assert "--verbose" in impl_argv and "--flag" in impl_argv


def test_passthrough_argv_threads_model_then_extra_args():
    config = HarnessConfig(
        name=HarnessName.CLAUDE_CODE, model="custom-model", extra_args=["--x", "1"]
    )

    assert get_harness(config)._passthrough_argv() == [
        "--model",
        "custom-model",
        "--x",
        "1",
    ]


def test_passthrough_argv_is_empty_without_model_or_extra_args():
    assert get_harness(HarnessConfig(name=HarnessName.CODEX))._passthrough_argv() == []

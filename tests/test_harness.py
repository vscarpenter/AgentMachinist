"""Tests for the harness abstraction layer."""

import subprocess
import time

import pytest

from machinist.config import HarnessConfig, HarnessName
from machinist.harness import get_harness
from machinist.harness.base import HarnessError


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
    assert ["--permission-mode", "plan"] == argv[argv.index("--permission-mode"):][:2]
    assert ["--tools", "Read,Grep,Glob"] == argv[argv.index("--tools"):][:2]
    assert "--no-session-persistence" in argv


def test_spec_argv_is_read_only_for_every_harness():
    # Phase 1 must not be able to edit files: stray edits would be swept
    # into the spec commit. Flags verified against the real CLIs 2026-08-16.
    expectations = {
        HarnessName.CLAUDE_CODE: ["--permission-mode", "plan", "--tools", "Read,Grep,Glob"],
        HarnessName.OPENCODE: ["--agent", "plan", "--pure"],
        HarnessName.PI: ["--tools", "read,grep,find,ls", "--no-extensions", "--no-session"],
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


def test_generate_spec_runs_in_cwd_with_spec_timeout(tmp_path):
    runner = FakeRunner(("the spec text", 0, ""))
    config = HarnessConfig(spec_timeout_minutes=5, timeout_minutes=45)
    harness = get_harness(config, runner=runner)

    output = harness.generate_spec("write a spec", cwd=tmp_path)

    assert output == "the spec text"
    args, kwargs = runner.calls[0]
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


def test_harness_subprocess_strips_controller_credentials_but_keeps_provider_key(tmp_path, monkeypatch):
    runner = FakeRunner(("ok", 0, ""))
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "actions-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "provider-secret")
    harness = get_harness(HarnessConfig(), runner=runner)

    harness.generate_spec("p", cwd=tmp_path)

    env = runner.calls[0][1]["env"]
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert env["ANTHROPIC_API_KEY"] == "provider-secret"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_adapters_publish_honest_policy_capabilities():
    for name in HarnessName:
        capability = get_harness(HarnessConfig(name=name)).capabilities
        assert capability.spec_repository_writes in {"cli-enforced", "advisory"}
        assert capability.implementation_git_control == "prompt-and-postcondition"

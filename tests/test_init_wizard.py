"""First-run Harness selection follows installed adapter capabilities."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import click
from click.testing import CliRunner

from machinist.cli import main
from machinist.config import load_config
from machinist.harness import HarnessRegistry
from machinist.harness.codex import Codex
from machinist.init_wizard import _ask_harness


class LocalPlugin(Codex):
    name = "local-plugin"
    default_command = "local-agent"
    descriptor = replace(Codex.descriptor, ci_spec=None)


class SpecOnly(LocalPlugin):
    name = "spec-only"
    descriptor = replace(LocalPlugin.descriptor, phases=frozenset({"spec"}))


def invoke_selection(monkeypatch, *, source="local", input="\n"):
    monkeypatch.setattr(
        "machinist.init_wizard.discover_harnesses",
        lambda: HarnessRegistry(
            {"codex": Codex, "local-plugin": LocalPlugin, "spec-only": SpecOnly}
        ),
    )
    monkeypatch.setattr(
        "machinist.init_wizard.shutil.which",
        lambda command: "/bin/local-agent" if command == "local-agent" else None,
    )

    @click.command()
    def choose():
        click.echo(
            "selected=" + _ask_harness(spec_source=source, manage_workflows=True)
        )

    return CliRunner().invoke(choose, input=input)


def test_wizard_defaults_to_installed_full_pipeline_plugin(monkeypatch):
    result = invoke_selection(monkeypatch)

    assert result.exit_code == 0, result.output
    assert "selected=local-plugin" in result.output
    assert "spec-only" not in result.output


def test_wizard_excludes_plugin_without_hosted_spec_support_for_actions(monkeypatch):
    result = invoke_selection(monkeypatch, source="github-actions")

    assert result.exit_code == 0, result.output
    assert "selected=codex" in result.output
    assert "local-plugin" not in result.output
    assert "spec-only" not in result.output


def install_plugin_fixture(monkeypatch, tmp_path):
    entry = SimpleNamespace(
        name="local-plugin", value="example:LocalPlugin", load=lambda: LocalPlugin
    )
    monkeypatch.setattr(
        "machinist.harness._selected_entry_points", lambda supplied: (entry,)
    )
    monkeypatch.setattr("machinist.cli._repository_root", lambda cwd: tmp_path)
    monkeypatch.setattr(
        "machinist.cli._bound_github_client",
        lambda *a, **k: SimpleNamespace(ensure_label=lambda *a, **k: None),
    )
    monkeypatch.chdir(tmp_path)


def test_onboard_flag_accepts_a_discovered_plugin(monkeypatch, tmp_path):
    install_plugin_fixture(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main, ["onboard", "--harness", "local-plugin", "--no-input"]
    )

    assert result.exit_code == 0, result.output
    assert load_config().harness.name == "local-plugin"


def test_onboard_rejects_incompatible_plugin_before_writing(monkeypatch, tmp_path):
    install_plugin_fixture(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "onboard",
            "--harness",
            "local-plugin",
            "--spec-source",
            "github-actions",
            "--no-input",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "cannot run the starter pipeline" in result.output
    assert not Path("machinist.yaml").exists()

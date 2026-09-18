"""Read-only aggregation of local and legacy Task histories."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from machinist.cli import main
from machinist.lifecycle import Phase, TaskLifecycle


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    return tmp_path


def test_report_reads_local_only_history_without_configuration_or_writes(repo):
    lifecycle = TaskLifecycle(repo / ".machinist/runs/local", repo_root=repo)
    lifecycle.run(1, Phase.EXECUTE, lambda claim: None)
    before = {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()}

    result = CliRunner().invoke(main, ["report", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["attempts"] == 1
    assert payload["by_source"] == {"legacy": 0, "local": 1}
    assert payload["task_counts"] == {"legacy": 0, "local": 1}
    assert {
        path: path.read_bytes() for path in repo.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize("source", ["all", "local", "legacy"])
def test_report_source_filter_does_not_alias_local_and_legacy_task_one(repo, source):
    for namespace in ("", "local"):
        TaskLifecycle(repo / ".machinist/runs" / namespace, repo_root=repo).run(
            1, Phase.SPEC, lambda claim: None
        )

    result = CliRunner().invoke(main, ["report", "--source", source, "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["attempts"] == (2 if source == "all" else 1)
    assert payload["task_counts"] == {
        "legacy": int(source != "local"),
        "local": int(source != "legacy"),
    }


def test_report_does_not_inherit_root_endpoint_for_local_data(repo, monkeypatch):
    Path("machinist.yaml").write_text(
        "telemetry:\n  otlp_endpoint: https://collector.example/metrics\n"
    )
    TaskLifecycle(repo / ".machinist/runs/local", repo_root=repo).run(
        1, Phase.SPEC, lambda claim: None
    )
    monkeypatch.setattr(
        "machinist.cli.export_otlp",
        lambda *a, **k: pytest.fail(
            "local Task data exported without explicit endpoint"
        ),
    )

    result = CliRunner().invoke(main, ["report", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["attempts"] == 1


def test_empty_report_does_not_create_runtime_storage(repo):
    result = CliRunner().invoke(main, ["report", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["attempts"] == 0
    assert not (repo / ".machinist").exists()


def test_explicit_local_export_requires_no_forge_and_omits_repository_identity(
    repo, monkeypatch
):
    exported = []
    TaskLifecycle(repo / ".machinist/runs/local", repo_root=repo).run(
        1, Phase.SPEC, lambda claim: claim.checkpoint(prompt="private objective")
    )
    monkeypatch.setattr(
        "machinist.cli.export_otlp", lambda *a, **k: exported.append((a, k))
    )

    runner = CliRunner()
    # Click before 8.2 mixes stderr into stdout unless explicitly disabled.
    if hasattr(runner, "mix_stderr"):
        runner.mix_stderr = False
    result = runner.invoke(
        main,
        [
            "report",
            "--source",
            "local",
            "--json",
            "--otlp-endpoint",
            "https://collector.example/metrics",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(exported) == 1
    payload = json.dumps(exported[0][0][1])
    assert "private objective" not in payload
    assert str(repo) not in payload
    assert "repository" not in payload
    assert json.loads(result.stdout)["attempts"] == 1
    assert result.stderr == (
        "Exported aggregate metrics to https://collector.example/metrics.\n"
    )


def test_legacy_source_retains_configured_telemetry_endpoint(repo, monkeypatch):
    Path("machinist.yaml").write_text(
        "telemetry:\n  otlp_endpoint: https://collector.example/metrics\n"
    )
    exported = []
    monkeypatch.setattr(
        "machinist.cli.export_otlp", lambda *a, **k: exported.append((a, k))
    )
    monkeypatch.setattr(
        "machinist.cli.Workspace.repository_identity", lambda self: "owner/repo"
    )

    result = CliRunner().invoke(main, ["report", "--source", "legacy", "--json"])

    assert result.exit_code == 0, result.output
    assert len(exported) == 1
    assert exported[0][0][0] == "https://collector.example/metrics"


def test_report_works_from_repository_subdirectory(repo, monkeypatch):
    TaskLifecycle(repo / ".machinist/runs/local", repo_root=repo).run(
        1, Phase.SPEC, lambda claim: None
    )
    nested = repo / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)

    result = CliRunner().invoke(main, ["report", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["attempts"] == 1


def test_report_rejects_symlinked_local_history_without_writing(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    runs = repo / ".machinist/runs"
    runs.mkdir(parents=True)
    (runs / "local").symlink_to(outside, target_is_directory=True)

    result = CliRunner().invoke(main, ["report", "--json"])

    assert result.exit_code == 1
    assert "unsafe" in result.output
    assert list(outside.iterdir()) == []

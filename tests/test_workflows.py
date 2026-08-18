"""Config-derived GitHub workflow projection and drift checks."""

import tomllib
from pathlib import Path

import pytest

from machinist.config import MachinistConfig, load_config
from machinist.workflows import WorkflowDriftError, expected_workflows, sync_workflows

_ROOT = Path(__file__).resolve().parent.parent


def _package_version() -> str:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def config(spec_source="github-actions"):
    return MachinistConfig.model_validate(
        {
            "github": {
                "spec_source": spec_source,
                "labels": {"trigger": "ai:task", "approved": "ship:it"},
            }
        }
    )


def test_render_uses_configured_labels_exact_command_and_pinned_version():
    rendered = expected_workflows(config(), installed_version="0.2.0")

    spec = rendered["machinist-spec.yml"]
    approval = rendered["machinist-approve.yml"]
    assert "github.event.label.name == 'ai:task'" in spec
    assert "agentmachinist==0.2.0" in spec
    assert "git+https://" not in spec
    assert '[[ "$NORMALIZED" == "/machinist-execute" ]]' in approval
    assert "ship:it" in approval
    assert "agentmachinist:approval sha=" in approval
    assert "pull_request_target:" in approval


def test_local_spec_source_omits_ci_dispatcher():
    assert set(expected_workflows(config("local"), installed_version="0.2.0")) == {
        "machinist-approve.yml"
    }


def test_write_is_deterministic_and_check_detects_drift(tmp_path):
    first = sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)
    second = sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)
    assert set(first.written) == {"machinist-spec.yml", "machinist-approve.yml"}
    assert second.written == ()

    target = tmp_path / ".github/workflows/machinist-approve.yml"
    target.write_text(target.read_text() + "# edited\n")
    with pytest.raises(WorkflowDriftError, match="machinist-approve.yml"):
        sync_workflows(tmp_path, config(), installed_version="0.2.0", check=True)


def test_switching_to_local_prunes_managed_spec_workflow(tmp_path):
    sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)
    report = sync_workflows(
        tmp_path, config("local"), installed_version="0.2.0", check=False
    )

    assert report.removed == ("machinist-spec.yml",)
    assert not (tmp_path / ".github/workflows/machinist-spec.yml").exists()


def test_checked_in_workflows_match_config_and_package_version():
    config_obj = load_config(_ROOT / "machinist.yaml")
    expected = expected_workflows(config_obj, installed_version=_package_version())
    directory = _ROOT / ".github" / "workflows"
    for name, wanted in expected.items():
        path = directory / name
        assert path.is_file(), f"missing managed workflow {name}"
        assert path.read_text() == wanted
    for name in ("machinist-spec.yml", "machinist-approve.yml"):
        if name not in expected:
            assert not (directory / name).exists()

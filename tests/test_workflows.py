"""Config-derived GitHub workflow projection and drift checks."""

import tomllib
from pathlib import Path

import pytest

from machinist.config import MachinistConfig, load_config
from machinist.managed_paths import ManagedPathError
from machinist.workflows import WorkflowDriftError, expected_workflows, sync_workflows

_ROOT = Path(__file__).resolve().parent.parent


def _package_version() -> str:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def config(spec_source="github-actions", *, manage_workflows=True):
    return MachinistConfig.model_validate(
        {
            "github": {
                "spec_source": spec_source,
                "manage_workflows": manage_workflows,
                "labels": {"trigger": "ai:task", "approved": "ship:it"},
            }
        }
    )


def test_render_binds_authorization_event_to_exact_sha_and_pinned_version():
    rendered = expected_workflows(config(), installed_version="0.2.0")

    spec = rendered["machinist-spec.yml"]
    approval = rendered["machinist-approve.yml"]
    assert spec.startswith("# agentmachinist-managed-sha256: ")
    assert approval.startswith("# agentmachinist-managed-sha256: ")
    assert "github.event.label.name == 'ai:task'" in spec
    assert "agentmachinist==0.2.0" in spec
    assert "persist-credentials: false" in spec
    assert "git+https://" not in spec
    assert "startsWith(github.event.comment.body, '/machinist-execute')" in approval
    assert "/machinist-execute[[:space:]]+([0-9a-fA-F]{40})" in approval
    assert "EVENT_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in approval
    assert '[[ "$CURRENT_SHA" == "$HEAD_SHA" ]]' in approval
    assert "ship:it" in approval
    assert "agentmachinist:approval sha=" in approval
    assert "pull_request_target:" in approval


def test_checkout_spec_install_uses_uv_run_from_the_repository():
    cfg = MachinistConfig.model_validate(
        {
            "github": {
                "spec_source": "github-actions",
                "spec_install": "checkout",
                "labels": {"trigger": "ai:task", "approved": "ship:it"},
            }
        }
    )
    spec = expected_workflows(cfg, installed_version="0.2.0")["machinist-spec.yml"]
    assert "uv sync --frozen" in spec
    assert "uv run machinist spec" in spec
    assert "uv tool install agentmachinist==" not in spec
    assert "git+https://" not in spec


def test_local_spec_source_omits_ci_dispatcher():
    assert set(expected_workflows(config("local"), installed_version="0.2.0")) == {
        "machinist-approve.yml"
    }


def test_unmanaged_mode_omits_all_managed_workflows():
    assert (
        expected_workflows(config(manage_workflows=False), installed_version="0.2.0")
        == {}
    )


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


def test_switching_to_unmanaged_prunes_every_managed_workflow(tmp_path):
    sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)

    report = sync_workflows(
        tmp_path,
        config(manage_workflows=False),
        installed_version="0.2.0",
        check=False,
    )

    assert set(report.removed) == {"machinist-spec.yml", "machinist-approve.yml"}
    assert not (tmp_path / ".github/workflows/machinist-spec.yml").exists()
    assert not (tmp_path / ".github/workflows/machinist-approve.yml").exists()


def test_unmanaged_mode_refuses_to_delete_user_authored_conventional_workflow(
    tmp_path,
):
    target = tmp_path / ".github/workflows/machinist-approve.yml"
    target.parent.mkdir(parents=True)
    target.write_text("name: My custom approval workflow\n")

    with pytest.raises(WorkflowDriftError, match="refusing to replace or remove"):
        sync_workflows(
            tmp_path,
            config(manage_workflows=False),
            installed_version="0.2.0",
            check=False,
        )

    assert target.read_text() == "name: My custom approval workflow\n"


def test_unmanaged_mode_refuses_to_delete_edited_managed_workflow(tmp_path):
    sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)
    target = tmp_path / ".github/workflows/machinist-approve.yml"
    target.write_text(target.read_text() + "# operator edit\n")

    with pytest.raises(WorkflowDriftError, match="refusing to replace or remove"):
        sync_workflows(
            tmp_path,
            config(manage_workflows=False),
            installed_version="0.2.0",
            check=False,
        )

    assert target.read_text().endswith("# operator edit\n")


@pytest.mark.parametrize(
    ("manage_workflows", "check"),
    [(True, False), (False, False), (False, True)],
)
def test_sync_rejects_managed_workflow_symlink_without_touching_target(
    tmp_path, manage_workflows, check
):
    outside = tmp_path / "outside.yml"
    outside.write_text("do not clobber\n")
    directory = tmp_path / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "machinist-approve.yml").symlink_to(outside)

    with pytest.raises(ManagedPathError, match="symbolic link"):
        sync_workflows(
            tmp_path,
            config(manage_workflows=manage_workflows),
            installed_version="0.2.0",
            check=check,
        )

    assert outside.read_text() == "do not clobber\n"


def test_sync_rejects_symlinked_workflow_parent_without_external_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".github").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedPathError, match="parent '.github'"):
        sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)

    assert list(outside.iterdir()) == []


def test_sync_rejects_non_regular_managed_target(tmp_path):
    target = tmp_path / ".github/workflows/machinist-approve.yml"
    target.mkdir(parents=True)

    with pytest.raises(ManagedPathError, match="not a regular file"):
        sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)


def test_sync_rejects_oversized_workflow_before_reading_payload(tmp_path):
    target = tmp_path / ".github/workflows/machinist-approve.yml"
    target.parent.mkdir(parents=True)
    with target.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024 + 1)

    with pytest.raises(ManagedPathError, match="exceeds"):
        sync_workflows(tmp_path, config(), installed_version="0.2.0", check=False)


@pytest.mark.skipif(
    not (_ROOT / ".github" / "workflows").exists(),
    reason="repository-only test (paths absent from sdist)",
)
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

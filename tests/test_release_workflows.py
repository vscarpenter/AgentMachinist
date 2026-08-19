"""Release and CI workflow safety contracts."""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = _ROOT / ".github" / "workflows"

if not _WORKFLOWS.exists():
    pytest.skip(
        "repository-only test (paths absent from sdist)", allow_module_level=True
    )


def _load_workflow(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _runs(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def _uses(job):
    return [step["uses"].split("@", 1)[0] for step in job["steps"] if "uses" in step]


def test_package_version_is_pep440_and_not_the_initial_release():
    version = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    assert version != "0.1.0"
    assert version[0].isdigit()
    assert "+" not in version


def test_release_hands_an_unprivileged_build_artifact_to_minimal_publish_job():
    workflow = _load_workflow("release.yml")
    jobs = workflow["jobs"]
    build = jobs["build"]
    publish = jobs["publish"]

    assert workflow["permissions"] == {}
    assert build["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["needs"] == "build"
    assert publish["environment"] == "pypi"
    assert "scripts/verify.sh" in _runs(build)
    assert "scripts/verify.sh" not in _runs(publish)
    assert "actions/upload-artifact" in _uses(build)
    assert "actions/download-artifact" in _uses(publish)
    assert "pypa/gh-action-pypi-publish" in _uses(publish)
    assert "actions/checkout" not in _uses(publish)
    assert "uv publish" not in (_WORKFLOWS / "release.yml").read_text()


def test_release_hashes_assets_and_smoke_tests_the_exact_published_version():
    workflow = _load_workflow("release.yml")
    jobs = workflow["jobs"]
    assert "sha256sum" in _runs(jobs["build"])
    assert "sha256sum --check" in _runs(jobs["publish"])
    assert jobs["release-assets"]["permissions"] == {"contents": "write"}
    assert set(jobs["release-assets"]["needs"]) == {"build", "publish"}
    release_env = jobs["release-assets"]["steps"][-1]["env"]
    assert release_env["GH_REPO"] == "${{ github.repository }}"
    assert jobs["verify-published"]["permissions"] == {}
    assert set(jobs["verify-published"]["needs"]) == {"build", "publish"}
    verify_commands = _runs(jobs["verify-published"])
    assert "pypi.org/pypi/agentmachinist/{version}/json" in verify_commands
    assert '--from "agentmachinist==$PACKAGE_VERSION"' in verify_commands


def test_ci_has_bounded_cross_platform_and_minimum_dependency_lanes():
    workflow = _load_workflow("ci.yml")
    jobs = workflow["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]
    gate = jobs["ci-gate"]

    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest"}
    assert set(matrix["python"]) == {"3.12", "3.13", "3.14"}
    assert all(job["timeout-minutes"] > 0 for job in jobs.values())
    assert "lowest-direct" in _runs(jobs["minimum-dependencies"])
    assert "uv sync --frozen" in _runs(jobs["minimum-dependencies"])
    quality_commands = _runs(jobs["quality"])
    assert "scripts/verify.sh format" in quality_commands
    assert "scripts/verify.sh lint" in quality_commands
    assert "scripts/verify.sh types" in quality_commands
    assert "scripts/verify.sh coverage" in _runs(jobs["coverage"])
    assert "scripts/verify.sh" in _runs(jobs["package"])
    assert gate["name"] == "CI gate"
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {
        "test",
        "minimum-dependencies",
        "quality",
        "coverage",
        "package",
    }
    assert gate["permissions"] == {}
    gate_commands = _runs(gate)
    assert 'test "$TEST_RESULT" = success' in gate_commands
    assert 'test "$MINIMUM_DEPENDENCIES_RESULT" = success' in gate_commands
    assert 'test "$QUALITY_RESULT" = success' in gate_commands
    assert 'test "$COVERAGE_RESULT" = success' in gate_commands
    assert 'test "$PACKAGE_RESULT" = success' in gate_commands


def test_all_third_party_actions_use_immutable_shas_with_version_comments():
    files = [
        _WORKFLOWS / "ci.yml",
        _WORKFLOWS / "release.yml",
        _WORKFLOWS / "machinist-spec.yml",
        _ROOT / "src" / "machinist" / "templates" / "github" / "machinist-spec.yml",
    ]
    pattern = re.compile(
        r"uses:\s+[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+@([0-9a-f]{40})\s+#\s+v\d"
    )
    for path in files:
        action_lines = [
            line.strip() for line in path.read_text().splitlines() if "uses:" in line
        ]
        assert action_lines
        assert all(pattern.search(line) for line in action_lines), path


def test_generated_claude_workflow_pins_tool_versions():
    text = (_WORKFLOWS / "machinist-spec.yml").read_text()
    assert "@anthropic-ai/claude-code@2.1.235" in text
    assert 'version: "0.12.5"' in text
    assert 'python-version: "3.12"' in text
    assert "persist-credentials: false" in text


def test_dependabot_groups_monthly_maintenance_for_a_solo_owner():
    config = yaml.safe_load((_ROOT / ".github" / "dependabot.yml").read_text())
    updates = config["updates"]
    assert {update["package-ecosystem"] for update in updates} == {
        "github-actions",
        "uv",
    }
    assert all(update["schedule"]["interval"] == "monthly" for update in updates)
    assert all(update["groups"] for update in updates)


def test_canonical_verify_refuses_test_induced_source_mutations_before_build():
    script = (_ROOT / "scripts" / "verify.sh").read_text()

    assert "before_status" in script
    assert "after_status" in script
    verify_all = script[
        script.index("verify_all()") : script.index(
            "\n}\n\ncase", script.index("verify_all()")
        )
    ]
    assert verify_all.index("after_status") < verify_all.index("build_and_smoke")
    assert "verification changed tracked or untracked source files" in script

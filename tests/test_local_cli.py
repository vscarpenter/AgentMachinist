"""The foreground journey keeps local work separate from explicit publication."""

import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from machinist.cli import main
from machinist.config import MachinistConfig
from machinist.forge import ExternalTask
from machinist.local_tasks import LocalTaskError
from machinist.local_workflow import LocalWorkflow
from machinist.managed_paths import ManagedPathError
from machinist.phases.execute import ExecutePhaseError
from machinist.phases.local import LocalPhaseError
from machinist.verification import VerificationError

SHA = "a" * 40


@pytest.fixture
def local_cli(monkeypatch, tmp_path):
    events = []
    task = SimpleNamespace(
        id="T1", number=1, title="Improve recovery", spec_sha=SHA, publication=None
    )

    class Workflow:
        store = object()
        lifecycle = object()

        def __init__(self):
            self.workspace = SimpleNamespace(origin_url=self.origin)

        def origin(self):
            events.append(("origin",))
            return "git@gitlab.example:team/subgroup/project.git"

        def start(self, title, body="", **kwargs):
            events.append(("start", title, body, kwargs))
            return task

        def approve(self, task_id, *, expected_sha, actor):
            events.append(("approve", task_id, expected_sha, actor))
            if expected_sha != SHA:
                raise LocalTaskError(
                    "Spec changed; inspect and approve the current SHA"
                )
            return task

        def continue_task(self, task_id):
            events.append(("continue", task_id))
            return task

        def retry(self, task_id, *, phase, resume=True):
            events.append(("retry", task_id, phase.value, resume))
            return task

        def amend(self, task_id, feedback):
            events.append(("amend", task_id, feedback))
            return task

        def integrate(self, task_id):
            events.append(("integrate", task_id))
            return task

        def cancel(
            self, task_id, reason="operator requested cancellation", *, clear=False
        ):
            events.append(("cancel", task_id, reason, clear))

        def status(self, task_id):
            return {
                "id": task_id,
                "title": task.title,
                "state": "awaiting approval",
                "spec_sha": SHA,
                "spec": "## Plan\nMake the recovery command actionable.\n",
                "candidate_sha": None,
                "report": None,
                "next_action": f"machinist approve --task T1 --spec-sha {SHA}",
            }

    workflow = Workflow()
    monkeypatch.setattr(
        "machinist.local_cli.find_repository_root", lambda cwd: tmp_path
    )
    monkeypatch.setattr(
        "machinist.local_cli.ensure_local_config", lambda *a, **k: MachinistConfig()
    )
    monkeypatch.setattr(
        "machinist.local_cli.load_local_config", lambda *a, **k: MachinistConfig()
    )
    monkeypatch.setattr("machinist.local_cli._workflow", lambda *a, **k: workflow)
    return SimpleNamespace(events=events, task=task, workflow=workflow, root=tmp_path)


def test_start_shows_spec_and_exact_approval_command_without_executing(local_cli):
    result = CliRunner().invoke(
        main, ["start", "Make configuration errors explain the recovery command"]
    )

    assert result.exit_code == 0, result.output
    assert local_cli.events == [
        ("start", "Make configuration errors explain the recovery command", "", {})
    ]
    assert "Make the recovery command actionable" in result.output
    assert f"approve --task T1 --spec-sha {SHA}" in result.output


def test_start_can_read_bounded_task_body_from_stdin(local_cli):
    result = CliRunner().invoke(
        main,
        ["start", "Fix recovery", "--body-file", "-"],
        input="Keep public behavior stable.\n",
    )

    assert result.exit_code == 0, result.output
    assert local_cli.events[0][2] == "Keep public behavior stable.\n"


def test_local_approval_requires_explicit_exact_spec_sha(local_cli):
    result = CliRunner().invoke(main, ["approve", "--task", "T1"])

    assert result.exit_code != 0
    assert "--spec-sha" in result.output
    assert local_cli.events == []


def test_local_approval_passes_exact_sha_and_rejects_stale_approval(local_cli):
    wrong = CliRunner().invoke(
        main, ["approve", "--task", "T1", "--spec-sha", "b" * 40]
    )
    right = CliRunner().invoke(main, ["approve", "--task", "T1", "--spec-sha", SHA])

    assert wrong.exit_code == 1
    assert "Spec changed" in wrong.output
    assert right.exit_code == 0, right.output
    assert local_cli.events[-1][:3] == ("approve", "T1", SHA)
    assert local_cli.events[-1][3]


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (["continue", "T1"], ("continue", "T1")),
        (["integrate", "T1"], ("integrate", "T1")),
        (
            ["retry", "--task", "T1", "--phase", "execute", "--fresh"],
            ("retry", "T1", "execute", False),
        ),
        (
            ["amend", "--task", "T1", "--feedback", "Clarify the error message"],
            ("amend", "T1", "Clarify the error message"),
        ),
        (
            ["cancel", "--task", "T1", "--clear"],
            ("cancel", "T1", "operator requested cancellation", True),
        ),
    ],
)
def test_local_actions_dispatch_without_github(local_cli, arguments, expected):
    result = CliRunner().invoke(main, arguments)

    assert result.exit_code == 0, result.output
    assert local_cli.events == [expected]


def test_status_accepts_local_task_id_without_remote_reads(local_cli):
    result = CliRunner().invoke(main, ["status", "T1", "--json"])

    assert result.exit_code == 0, result.output
    assert '"id": "T1"' in result.output
    assert local_cli.events == []


def test_status_reopens_the_spec_to_review_before_approval(local_cli):
    result = CliRunner().invoke(main, ["status", "T1"])

    assert result.exit_code == 0, result.output
    assert "Make the recovery command actionable" in result.output
    assert f"approve --task T1 --spec-sha {SHA}" in result.output


def test_local_and_legacy_approval_targets_cannot_be_mixed(local_cli):
    result = CliRunner().invoke(
        main, ["approve", "--task", "T1", "--issue", "1", "--spec-sha", SHA]
    )

    assert result.exit_code != 0
    assert local_cli.events == []


def test_publication_binds_nested_namespace_origin_and_runs_only_explicitly(
    local_cli, monkeypatch
):
    def forge(provider, repository, host):
        local_cli.events.append(("forge", provider, repository, host))
        return "bound-forge"

    def publish(task_id, **kwargs):
        assert kwargs["store"] is local_cli.workflow.store
        assert kwargs["forge"] == "bound-forge"
        local_cli.events.append(("publish", task_id))
        local_cli.task.publication = {
            "url": "https://gitlab.example/team/subgroup/project/-/merge_requests/7"
        }
        return local_cli.task

    monkeypatch.setattr("machinist.local_cli._forge_client", forge)
    monkeypatch.setattr("machinist.local_cli.publish_task", publish)
    result = CliRunner().invoke(
        main, ["publish", "T1", "--provider", "gitlab", "--host", "gitlab.example"]
    )

    assert result.exit_code == 0, result.output
    assert local_cli.events == [
        ("origin",),
        ("forge", "gitlab", "team/subgroup/project", "gitlab.example"),
        ("publish", "T1"),
    ]
    assert "merge_requests/7" in result.output


def test_publication_rejects_host_mismatch_before_remote_client(local_cli, monkeypatch):
    monkeypatch.setattr(
        "machinist.local_cli._forge_client", lambda *a: pytest.fail("no remote calls")
    )
    result = CliRunner().invoke(
        main, ["publish", "T1", "--provider", "gitlab", "--host", "other.example"]
    )

    assert result.exit_code == 1
    assert "host" in result.output
    assert local_cli.events == [("origin",)]


def test_start_imports_gitlab_issue_url_with_nested_namespace(local_cli, monkeypatch):
    url = "https://gitlab.example/team/subgroup/project/-/issues/42"

    def forge(provider, repository, host):
        local_cli.events.append(("import-client", provider, repository, host))
        return SimpleNamespace(
            get_issue=lambda number: ExternalTask(
                provider, host, repository, number, "Fix recovery", "Issue details", url
            )
        )

    monkeypatch.setattr("machinist.local_cli._forge_client", forge)
    result = CliRunner().invoke(
        main, ["start", "--from-issue", url, "--provider", "gitlab"]
    )

    assert result.exit_code == 0, result.output
    assert local_cli.events[0] == (
        "import-client",
        "gitlab",
        "team/subgroup/project",
        "gitlab.example",
    )
    assert local_cli.events[1][:3] == ("start", "Fix recovery", "Issue details")
    assert local_cli.events[1][3]["source"]["url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.example/group/project/-/issues/1",
        "https://user:secret@gitlab.example/group/project/-/issues/1",
        "https://gitlab.example/group/project/-/issues/1?token=secret",
    ],
)
def test_import_rejects_unsafe_url_before_remote_calls(local_cli, monkeypatch, url):
    monkeypatch.setattr(
        "machinist.local_cli._forge_client", lambda *a: pytest.fail("no remote calls")
    )
    result = CliRunner().invoke(
        main, ["start", "--from-issue", url, "--provider", "gitlab"]
    )

    assert result.exit_code != 0
    assert local_cli.events == []


@pytest.fixture
def legacy_intake(monkeypatch, tmp_path):
    from machinist.github import GitHubError

    monkeypatch.chdir(tmp_path)
    (tmp_path / "machinist.yaml").write_text("version: 1\n")
    created = []
    fake = SimpleNamespace()
    fake.error = None

    def create_issue(*, title, body):
        if fake.error:
            raise GitHubError(fake.error)
        created.append((title, body))
        return SimpleNamespace(
            number=42, url="https://github.com/example/project/issues/42"
        )

    fake.create_issue = create_issue
    fake.add_issue_label = lambda *args: None
    monkeypatch.setattr("machinist.cli._bound_github_client", lambda *a, **k: fake)
    body = """## Objective
Make authentication failures name the exact recovery command.

## Acceptance criteria
- [ ] Error names the missing credential

## Constraints
Preserve API compatibility.

## Verification
Run pytest.
"""
    return SimpleNamespace(root=tmp_path, created=created, fake=fake, body=body)


def test_legacy_task_new_reads_stdin_without_prompts(legacy_intake):
    result = CliRunner().invoke(
        main,
        ["task", "new", "--title", "Improve recovery", "--body-file", "-"],
        input=legacy_intake.body,
    )

    assert result.exit_code == 0, result.output
    assert legacy_intake.created == [("Improve recovery", legacy_intake.body)]
    assert "5 prompts" not in result.output


@pytest.mark.parametrize("source", ["local", "github-actions"])
@pytest.mark.parametrize("dispatch", [False, True])
def test_new_issue_suggests_the_configured_next_activity(
    legacy_intake, source, dispatch
):
    (legacy_intake.root / "machinist.yaml").write_text(
        f"version: 1\ngithub:\n  spec_source: {source}\n"
    )
    labels = []
    legacy_intake.fake.add_issue_label = lambda *args: labels.append(args)
    result = CliRunner().invoke(
        main,
        ["task", "new", "--title", "Improve recovery", "--body-file", "-"]
        + (["--dispatch"] if dispatch else []),
        input=legacy_intake.body,
    )

    assert result.exit_code == 0, result.output
    assert "Next:" in result.output
    assert "machinist task lint" not in result.output
    assert len(legacy_intake.created) == 1
    assert labels == ([(42, "agent-task")] if dispatch else [])
    if source == "local":
        command = "machinist watch --once -v" if dispatch else "machinist spec 42"
        assert command in result.output
    else:
        assert "GitHub Actions" in result.output
        assert "machinist explain 42" in result.output
        assert "machinist spec 42" not in result.output
        assert "machinist watch" not in result.output
        if not dispatch:
            assert (
                "gh issue edit https://github.com/example/project/issues/42 "
                "--add-label agent-task"
            ) in result.output


def test_hosted_dispatch_hint_quotes_custom_label_and_binds_issue_url(legacy_intake):
    label = "team: ready for spec"
    (legacy_intake.root / "machinist.yaml").write_text(
        "version: 1\ngithub:\n  spec_source: github-actions\n"
        f"  labels:\n    trigger: {json.dumps(label)}\n"
    )
    result = CliRunner().invoke(
        main,
        ["task", "new", "--title", "Improve recovery", "--body-file", "-"],
        input=legacy_intake.body,
    )

    assert result.exit_code == 0, result.output
    command = next(
        line.strip() for line in result.output.splitlines() if "gh issue edit" in line
    )
    assert shlex.split(command) == [
        "gh",
        "issue",
        "edit",
        "https://github.com/example/project/issues/42",
        "--add-label",
        label,
    ]


@pytest.mark.parametrize("as_json", [False, True])
def test_integrated_status_offers_optional_complete_publication_commands(
    local_cli, monkeypatch, as_json
):
    payload = {
        "id": "T1",
        "title": "Improve recovery",
        "state": "integrated",
        "next_action": "Local integration complete. Publication is optional.",
    }
    monkeypatch.setattr(local_cli.workflow, "status", lambda task_id: payload)
    result = CliRunner().invoke(
        main, ["status", "T1"] + (["--json"] if as_json else [])
    )

    assert result.exit_code == 0, result.output
    assert local_cli.events == []
    if as_json:
        assert json.loads(result.output) == payload
    else:
        assert "Local integration complete" in result.output
        assert "Optional" in result.output
        assert "machinist publish T1 --provider github" in result.output
        assert "machinist publish T1 --provider gitlab" in result.output


def test_legacy_intake_lint_failure_retains_exact_draft(legacy_intake):
    body = "## Objective\nIncomplete task\n"
    result = CliRunner().invoke(
        main,
        ["task", "new", "--title", "Improve recovery", "--body-file", "-"],
        input=body,
    )

    assert result.exit_code == 1, result.output
    drafts = list((legacy_intake.root / ".machinist/runs/intake").glob("*.md"))
    assert len(drafts) == 1
    assert drafts[0].read_text() == body
    assert str(drafts[0]) in result.output
    assert legacy_intake.created == []


def test_legacy_intake_remote_failure_retains_exact_draft(legacy_intake):
    legacy_intake.fake.error = "GitHub unavailable"
    result = CliRunner().invoke(
        main,
        ["task", "new", "--title", "Improve recovery", "--body-file", "-"],
        input=legacy_intake.body,
    )

    assert result.exit_code == 1
    drafts = list((legacy_intake.root / ".machinist/runs/intake").glob("*.md"))
    assert len(drafts) == 1
    assert drafts[0].read_text() == legacy_intake.body
    assert str(drafts[0]) in result.output


def test_legacy_task_new_checks_configuration_before_prompting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["task", "new", "--title", "Improve recovery"])

    assert result.exit_code == 1
    assert "machinist.yaml" in result.output
    assert "Objective —" not in result.output


@pytest.mark.parametrize(
    "error_type",
    [LocalPhaseError, ExecutePhaseError, ManagedPathError, VerificationError],
)
def test_local_phase_failures_render_actionable_errors(
    local_cli, monkeypatch, error_type
):
    def fail(task_id):
        raise error_type("Verification failed; inspect the retained Workshop")

    monkeypatch.setattr(local_cli.workflow, "continue_task", fail)
    result = CliRunner().invoke(main, ["continue", "T1"])

    assert result.exit_code == 1
    assert "Error: Verification failed; inspect the retained Workshop" in result.output


def test_main_help_leads_with_local_first_task():
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "Start with 'machinist start OBJECTIVE'" in result.output
    assert "optional GitHub" in result.output
    assert result.output.index("Tasks  ") < result.output.index("Setup  ")


def test_real_foreground_workflow_preserves_checkout_until_explicit_integration(
    monkeypatch, tmp_path
):
    root = tmp_path / "repository"
    root.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (root / ".gitignore").write_text("__pycache__/\n")
    (root / "feature.py").write_text("def answer():\n    return 1\n")
    (root / "test_feature.py").write_text(
        "import unittest\nfrom feature import answer\nclass TestFeature(unittest.TestCase):\n    def test_answer(self): self.assertEqual(answer(), 1)\n"
    )
    git("add", ".")
    git("commit", "-m", "baseline")
    baseline = git("rev-parse", "HEAD")
    config = MachinistConfig.model_validate(
        {
            "workspace": {"root": str(tmp_path / "workshops")},
            "tests": {"command": f"{shlex.quote(sys.executable)} -m unittest discover"},
        }
    )
    calls = []

    class Harness:
        name = "fake"

        def generate_spec(self, prompt, cwd):
            calls.append("spec")
            return "# Spec\n\nReturn two from answer and update its regression test.\n"

        def implement(self, prompt, cwd):
            calls.append("execute")
            (cwd / "feature.py").write_text("def answer():\n    return 2\n")
            target = cwd / "test_feature.py"
            target.write_text(target.read_text().replace("answer(), 1", "answer(), 2"))
            return "Updated the answer and test."

        def review(self, prompt, cwd):
            calls.append("review")
            return json.dumps(
                {"version": 1, "summary": "Matches the Spec", "findings": []}
            )

    harness = Harness()
    harness.config = config.harness
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        "machinist.local_cli.ensure_local_config", lambda *a, **k: config
    )
    monkeypatch.setattr("machinist.local_cli.load_local_config", lambda *a, **k: config)
    monkeypatch.setattr(
        "machinist.local_cli.LocalWorkflow",
        lambda *a, **k: LocalWorkflow(
            *a, **k, harness_factory=lambda phase, number: harness
        ),
    )
    monkeypatch.setattr(
        "machinist.local_cli._forge_client",
        lambda *a, **k: pytest.fail("local flow accessed a forge"),
    )
    runner = CliRunner()

    start = runner.invoke(
        main, ["start", "Improve the answer with its regression test"]
    )
    assert start.exit_code == 0, start.output
    assert calls == ["spec"]
    status = runner.invoke(main, ["status", "T1", "--json"])
    snapshot = json.loads(status.output)
    spec_sha = snapshot["spec_sha"]
    assert f"approve --task T1 --spec-sha {spec_sha}" in start.output
    assert git("rev-parse", "HEAD") == baseline
    rejected = runner.invoke(main, ["approve", "--task", "T1", "--spec-sha", "f" * 40])
    assert rejected.exit_code == 1 and calls == ["spec"]
    approved = runner.invoke(main, ["approve", "--task", "T1", "--spec-sha", spec_sha])
    assert approved.exit_code == 0, approved.output
    assert "ready to integrate" in approved.output
    assert calls == ["spec", "execute", "review"]
    assert git("rev-parse", "HEAD") == baseline
    integrated = runner.invoke(main, ["integrate", "T1"])
    assert integrated.exit_code == 0, integrated.output
    assert git("rev-parse", "HEAD") != baseline
    assert git("status", "--porcelain") == ""
    assert git("remote") == ""
    assert "return 2" in (root / "feature.py").read_text()

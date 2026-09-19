"""Publication retries use durable Git intent without repeating local Phases."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from machinist.authorization import (
    authorization_evidence,
    bind_delegation_spec,
    make_delegation,
)
from machinist.config import MachinistConfig
from machinist.forge import PublishedChange
from machinist.lifecycle import Phase, TaskLifecycle
from machinist.local_tasks import LocalTaskStore
from machinist.publication import PublicationError, publish_task

SPEC = "a" * 40
CANDIDATE = "b" * 40
OTHER = "c" * 40


@dataclass(frozen=True)
class Task:
    repository: str
    number: int = 1
    id: str = "T1"
    title: str = "Handle an invalid timezone without crashing"
    body: str = "Task objective and acceptance criteria"
    branch: str = "machinist/task-1"
    base_branch: str = "main"
    base_sha: str = "d" * 40
    spec_sha: str | None = SPEC
    candidate_sha: str | None = CANDIDATE
    approval: dict | None = None
    delegation: dict | None = None
    source: dict | None = None
    feedback: str | None = None
    spec_base_sha: str | None = None
    review_report: dict | None = None
    publication: dict | None = None


class Store:
    def __init__(self, task, events):
        self.task = task
        self.events = events

    @contextmanager
    def claim(self, task_id):
        assert task_id == self.task.id
        self.events.append("claim")
        yield self.task

    def update(self, task, **changes):
        assert task is self.task
        self.task = replace(task, **changes)
        self.events.append(("checkpoint", dict(self.task.publication)))
        return self.task


class Workshop:
    def __init__(self, repo_root, events):
        self.repo_root = repo_root
        self.events = events
        self.origin = "git@gitlab.com:team/subgroup/demo.git"
        self.local_sha = CANDIDATE
        self.remote = None
        self.fail_after_push = False

    def branch_sha(self, branch):
        return self.local_sha

    def origin_url(self):
        return self.origin

    def bind_publication_auth(self, provider, *, origin_url):
        assert provider in {"github", "gitlab"}
        assert origin_url == self.origin

    def remote_sha(self, branch, *, origin_url):
        assert origin_url == self.origin
        self.events.append("remote-read")
        return self.remote

    def push_candidate(
        self, branch, *, expected_candidate_sha, expected_remote_sha, origin_url
    ):
        assert expected_remote_sha == self.remote
        assert expected_candidate_sha == self.local_sha
        assert origin_url == self.origin
        self.events.append("push")
        self.remote = expected_candidate_sha
        if self.fail_after_push:
            self.fail_after_push = False
            raise OSError("uncertain push result")
        return expected_candidate_sha


class Forge:
    provider = "gitlab"
    host = "gitlab.com"
    repository = "team/subgroup/demo"

    def __init__(self, workshop, events):
        self.workshop = workshop
        self.events = events
        self.change = None
        self.fail_after_create = False
        self.body = ""

    def find_change(self, branch):
        self.events.append("find-change")
        if self.change is not None:
            self.change = replace(self.change, head_sha=self.workshop.remote)
        return self.change

    def create_change(
        self, *, branch, base, title, body, draft=True, cancel_check=None
    ):
        self.events.append("create-change")
        self.body = body
        self.change = PublishedChange(
            self.provider,
            self.host,
            self.repository,
            9,
            "https://gitlab.com/team/subgroup/demo/-/merge_requests/9",
            branch,
            base,
            self.workshop.remote,
            "OPEN",
            draft,
        )
        if self.fail_after_create:
            self.fail_after_create = False
            raise OSError("uncertain create result")
        return self.change

    def update_change(self, number, *, title, body, draft, cancel_check=None):
        self.events.append("update-change")
        self.body = body
        self.change = replace(self.change, is_draft=draft)
        return self.change

    def get_change(self, number):
        return self.change


@pytest.fixture
def ready(tmp_path):
    events = []
    task = Task(
        repository=str(tmp_path),
        approval={"repository": str(tmp_path), "task_id": "T1", "spec_sha": SPEC},
        review_report={"completed": True, "reviewed_sha": CANDIDATE, "findings": []},
    )
    store = Store(task, events)
    workspace = Workshop(tmp_path, events)
    forge = Forge(workspace, events)
    lifecycle = TaskLifecycle(tmp_path / ".machinist/runs/local")
    lifecycle.run(
        1,
        Phase.EXECUTE,
        lambda claim: claim.checkpoint(implementation_sha=CANDIDATE, approved_sha=SPEC),
    )
    lifecycle.run(
        1, Phase.REVIEW, lambda claim: claim.checkpoint(reviewed_sha=CANDIDATE)
    )
    return store, workspace, forge, events


def publish(ready):
    store, workspace, forge, _ = ready
    return publish_task("T1", store=store, workspace=workspace, forge=forge)


def test_publish_records_intent_before_push_and_returns_exact_reviewed_change(ready):
    task = publish(ready)
    _, workspace, forge, events = ready

    checkpoint = next(event for event in events if isinstance(event, tuple))
    assert events.index(checkpoint) < events.index("push")
    assert checkpoint[1]["intended_sha"] == CANDIDATE
    assert checkpoint[1]["expected_remote_sha"] is None
    assert task.publication["published_sha"] == CANDIDATE
    assert task.publication["change_number"] == 9
    assert task.publication["stage"] == "published"
    assert workspace.remote == CANDIDATE
    assert not forge.change.is_draft


def test_background_publication_persists_draft_intent_and_remains_draft(ready):
    store, workspace, forge, events = ready
    task = publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)

    checkpoint = next(event for event in events if isinstance(event, tuple))
    assert checkpoint[1]["draft"] is True
    assert task.publication["draft"] is True
    assert forge.change.is_draft


def test_uncertain_draft_publication_cannot_retry_as_ready(ready):
    store, workspace, forge, events = ready
    forge.fail_after_create = True
    with pytest.raises(OSError, match="uncertain create"):
        publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)

    with pytest.raises(PublicationError, match="draft intent"):
        publish(ready)

    assert events.count("create-change") == 1
    assert "update-change" not in events
    assert forge.change.is_draft


def test_reconciled_publication_does_not_reverse_subsequent_ready_transition(ready):
    store, workspace, forge, events = ready
    task = publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)
    forge.change = replace(forge.change, is_draft=False)
    events.clear()

    assert (
        publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)
        == task
    )
    assert not forge.change.is_draft
    assert "update-change" not in events


@pytest.mark.parametrize("after", [None, "push", "create-change"])
def test_cancellation_stops_before_next_external_publication_mutation(ready, after):
    store, workspace, forge, events = ready

    with pytest.raises(PublicationError, match="cancelled"):
        publish_task(
            "T1",
            store=store,
            workspace=workspace,
            forge=forge,
            draft=True,
            cancel_check=lambda: after is None or after in events,
        )

    if after is None:
        assert "push" not in events
    if after != "create-change":
        assert "create-change" not in events
    assert "update-change" not in events


@pytest.fixture
def delegated(ready):
    store, workspace, _, _ = ready
    task = replace(
        store.task,
        approval=None,
        spec_sha=None,
        candidate_sha=None,
        source={"provider": "github", "number": 42, "event_id": "queue-17"},
    )
    config = MachinistConfig()
    task = replace(
        task,
        delegation=make_delegation(
            task, config, actor="vinny", source_event="queue-17"
        ),
        spec_sha=SPEC,
        candidate_sha=CANDIDATE,
    )
    task = replace(task, delegation=bind_delegation_spec(task, config))
    store.task = task
    authorization = authorization_evidence(task)
    runs = TaskLifecycle(workspace.repo_root / ".machinist/runs/local")
    runs.run(
        1,
        Phase.EXECUTE,
        lambda claim: claim.checkpoint(
            implementation_sha=CANDIDATE,
            approved_sha=SPEC,
            authorization=authorization,
            verification_report={
                "success": True,
                "gates": [
                    {
                        "name": "tests",
                        "required": True,
                        "passed": True,
                        "status": "passed",
                        "stdout_log": str(
                            workspace.repo_root / ".machinist/private.log"
                        ),
                    }
                ],
            },
        ),
        repeat_succeeded_if=lambda record: True,
    )
    runs.run(
        1,
        Phase.REVIEW,
        lambda claim: claim.checkpoint(
            reviewed_sha=CANDIDATE,
            authorization=authorization,
            review_report=task.review_report,
        ),
        repeat_succeeded_if=lambda record: True,
    )
    return ready


def test_delegated_publication_uses_task_authorization_and_readable_evidence(delegated):
    store, workspace, forge, _ = delegated
    store.task = replace(
        store.task,
        review_report={
            **store.task.review_report,
            "summary": "Timezone parsing now returns a helpful error.",
            "findings": [
                {
                    "severity": "high",
                    "file": "timezone.py",
                    "line": 4,
                    "message": "The fallback needs another test.",
                    "remediation": "Cover the fallback path.",
                }
            ],
        },
    )
    runs = TaskLifecycle(workspace.repo_root / ".machinist/runs/local")
    runs.run(
        1,
        Phase.REVIEW,
        lambda claim: claim.checkpoint(
            reviewed_sha=CANDIDATE,
            authorization=authorization_evidence(store.task),
            review_report=store.task.review_report,
        ),
        repeat_succeeded_if=lambda record: True,
    )
    task = publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)

    assert task.approval is None
    assert forge.change.is_draft
    assert "Timezone parsing now returns a helpful error." in forge.body
    assert "tests" in forge.body and "passed" in forge.body
    assert "The fallback needs another test." in forge.body
    assert "high" in forge.body.lower()
    assert "vinny" in forge.body
    assert "approved Spec" not in forge.body
    assert str(workspace.repo_root) not in forge.body


@pytest.mark.parametrize("phase", [Phase.EXECUTE, Phase.REVIEW])
def test_delegated_publication_requires_matching_authorization_evidence(
    delegated, phase
):
    store, workspace, forge, _ = delegated
    runs = TaskLifecycle(workspace.repo_root / ".machinist/runs/local")
    evidence = dict(runs.record(1, phase).evidence)
    evidence.pop("authorization")
    runs.run(
        1,
        phase,
        lambda claim: claim.checkpoint(**evidence),
        repeat_succeeded_if=lambda record: True,
    )

    with pytest.raises(PublicationError, match="authorization"):
        publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)


def test_delegated_publication_cannot_bypass_ci_by_requesting_ready(delegated):
    with pytest.raises(PublicationError, match="draft"):
        publish(delegated)
    assert "push" not in delegated[3]


@pytest.mark.parametrize(
    "verification",
    [
        None,
        {"success": False, "gates": []},
        {"success": True, "gates": []},
        {"success": True, "gates": [{"required": True, "passed": False}]},
    ],
)
def test_delegated_publication_requires_successful_required_gates(
    delegated, verification
):
    store, workspace, forge, _ = delegated
    runs = TaskLifecycle(workspace.repo_root / ".machinist/runs/local")
    evidence = dict(runs.record(1, Phase.EXECUTE).evidence)
    evidence["verification_report"] = verification
    runs.run(
        1,
        Phase.EXECUTE,
        lambda claim: claim.checkpoint(**evidence),
        repeat_succeeded_if=lambda record: True,
    )

    with pytest.raises(PublicationError, match="Verification"):
        publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)


def test_delegated_publication_cannot_hide_findings_by_replacing_task_report(delegated):
    store, workspace, forge, _ = delegated
    store.task = replace(
        store.task, review_report={**store.task.review_report, "summary": "Changed"}
    )

    with pytest.raises(PublicationError, match="Review report"):
        publish_task("T1", store=store, workspace=workspace, forge=forge, draft=True)


@pytest.mark.parametrize(
    "origin",
    [
        "https://gitlab.com/team/subgroup/demo.git",
        "ssh://git@gitlab.com/team/subgroup/demo.git",
        "ssh://git@gitlab.com:2222/team/subgroup/demo.git",
        "git@gitlab.com:team/subgroup/demo.git",
    ],
)
def test_publication_binds_supported_git_transports(ready, origin):
    ready[1].origin = origin

    assert publish(ready).publication["repository"] == "team/subgroup/demo"


def test_https_origin_port_is_part_of_the_bound_api_host(ready):
    ready[1].origin = "https://gitlab.com:8443/team/subgroup/demo.git"
    ready[2].host = "gitlab.com:8443"

    assert publish(ready).publication["host"] == "gitlab.com:8443"


def test_private_https_git_uses_credentials_refreshed_by_forge_lookup(
    ready, monkeypatch
):
    _, workspace, forge, _ = ready
    workspace.origin = "https://gitlab.com/team/subgroup/demo.git"
    refreshed = False
    find = forge.find_change
    remote = workspace.remote_sha

    def authenticated_find(branch):
        nonlocal refreshed
        refreshed = True
        return find(branch)

    def private_remote(branch, *, origin_url):
        if not refreshed:
            raise PermissionError("stored OAuth credential needs refresh")
        return remote(branch, origin_url=origin_url)

    monkeypatch.setattr(forge, "find_change", authenticated_find)
    monkeypatch.setattr(workspace, "remote_sha", private_remote)

    assert publish(ready).publication["stage"] == "published"


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/team/subgroup/demo.git",
        "https://gitlab.com/other/demo.git",
        "https://token@gitlab.com/team/subgroup/demo.git",
        "https://gitlab.com/team/subgroup/demo.git?token=secret",
        "/tmp/local-bare.git",
    ],
)
def test_wrong_or_unsafe_origin_refuses_before_remote_calls(ready, origin):
    ready[1].origin = origin

    with pytest.raises(PublicationError, match="origin"):
        publish(ready)

    assert ready[3] == ["claim"]


@pytest.mark.parametrize(
    "changes",
    [
        {"approval": None},
        {"review_report": None},
        {"review_report": {"completed": False, "reviewed_sha": CANDIDATE}},
        {"review_report": {"completed": True, "reviewed_sha": OTHER}},
        {"candidate_sha": None},
    ],
)
def test_publication_requires_approved_spec_and_completed_exact_review(ready, changes):
    ready[0].task = replace(ready[0].task, **changes)

    with pytest.raises(PublicationError):
        publish(ready)

    assert ready[3] == ["claim"]


def test_publication_requires_current_candidate_branch(ready):
    ready[1].local_sha = OTHER

    with pytest.raises(PublicationError, match="candidate"):
        publish(ready)

    assert ready[3] == ["claim"]


def test_task_report_does_not_replace_durable_phase_evidence(ready):
    runs = Path(ready[0].task.repository) / ".machinist/runs/local"
    (runs / "issue-1-review.json").unlink()

    with pytest.raises(PublicationError, match="Review"):
        publish(ready)

    assert ready[3] == ["claim"]


@pytest.mark.parametrize("remote", [CANDIDATE, OTHER])
def test_first_publication_refuses_an_unowned_remote_branch(ready, remote):
    ready[1].remote = remote

    with pytest.raises(PublicationError, match="unowned"):
        publish(ready)

    assert "push" not in ready[3]
    assert "create-change" not in ready[3]


def test_uncertain_push_reconciles_intended_sha_without_repeating_push(ready):
    ready[1].fail_after_push = True
    with pytest.raises(OSError, match="uncertain push"):
        publish(ready)

    task = publish(ready)

    assert task.publication["stage"] == "published"
    assert ready[3].count("push") == 1
    assert ready[3].count("create-change") == 1


def test_uncertain_creation_reuses_existing_change_without_duplicate(ready):
    ready[2].fail_after_create = True
    with pytest.raises(OSError, match="uncertain create"):
        publish(ready)

    task = publish(ready)

    assert task.publication["change_number"] == 9
    assert ready[3].count("push") == 1
    assert ready[3].count("create-change") == 1


def test_retry_refuses_concurrent_remote_change(ready):
    ready[1].fail_after_push = True
    with pytest.raises(OSError):
        publish(ready)
    ready[1].remote = OTHER

    with pytest.raises(PublicationError, match="remote branch changed"):
        publish(ready)

    assert ready[3].count("push") == 1
    assert "create-change" not in ready[3]


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_closed_publication_cannot_be_reused(ready, state):
    publish(ready)
    ready[2].change = replace(ready[2].change, state=state)
    ready[3].clear()

    with pytest.raises(PublicationError, match="state"):
        publish(ready)

    assert "push" not in ready[3]
    assert "update-change" not in ready[3]


def test_publication_binding_cannot_be_switched_on_retry(ready):
    publish(ready)
    ready[2].repository = "other/demo"
    ready[1].origin = "git@gitlab.com:other/demo.git"
    ready[3].clear()

    with pytest.raises(PublicationError, match="binding"):
        publish(ready)

    assert ready[3] == ["claim"]


def replace_candidate(ready, sha):
    store, workspace, _, _ = ready
    store.task = replace(
        store.task,
        candidate_sha=sha,
        review_report={"completed": True, "reviewed_sha": sha, "findings": []},
    )
    workspace.local_sha = sha
    lifecycle = TaskLifecycle(workspace.repo_root / ".machinist/runs/local")
    lifecycle.run(
        1,
        Phase.EXECUTE,
        lambda claim: claim.checkpoint(implementation_sha=sha, approved_sha=SPEC),
        repeat_succeeded_if=lambda record: True,
    )
    lifecycle.run(
        1,
        Phase.REVIEW,
        lambda claim: claim.checkpoint(reviewed_sha=sha),
        repeat_succeeded_if=lambda record: True,
    )


def test_amended_reviewed_candidate_uses_last_owned_publication_as_lease(ready):
    publish(ready)
    replace_candidate(ready, OTHER)
    ready[3].clear()

    task = publish(ready)

    checkpoint = next(event for event in ready[3] if isinstance(event, tuple))
    assert checkpoint[1]["expected_remote_sha"] == CANDIDATE
    assert checkpoint[1]["intended_sha"] == OTHER
    assert task.publication["published_sha"] == OTHER
    assert task.publication["change_number"] == 9
    assert ready[3].count("push") == 1
    assert "create-change" not in ready[3]


def test_new_candidate_cannot_replace_unreconciled_publication_intent(ready):
    ready[1].fail_after_push = True
    with pytest.raises(OSError):
        publish(ready)
    replace_candidate(ready, OTHER)
    ready[3].clear()

    with pytest.raises(PublicationError, match="reconcile the pending"):
        publish(ready)

    assert "push" not in ready[3]
    assert "create-change" not in ready[3]


def test_successful_publication_still_refuses_a_replaced_change_number(ready):
    publish(ready)
    ready[2].change = replace(ready[2].change, number=10)
    ready[3].clear()

    with pytest.raises(PublicationError, match="number"):
        publish(ready)

    assert "push" not in ready[3]
    assert "update-change" not in ready[3]


def test_durable_store_round_trip_keeps_publication_independent_from_local_runs(ready):
    fake_store, workspace, forge, _ = ready
    original = fake_store.task
    store = LocalTaskStore(workspace.repo_root)
    task = store.create(
        original.title,
        original.body,
        original.base_branch,
        original.base_sha,
        "machinist/",
    )
    store.update(
        task,
        spec_sha=original.spec_sha,
        candidate_sha=original.candidate_sha,
        approval=original.approval,
        review_report=original.review_report,
    )
    runs = TaskLifecycle(workspace.repo_root / ".machinist/runs/local")
    before = runs.inventory()

    published = publish_task("T1", store=store, workspace=workspace, forge=forge)
    retried = publish_task("T1", store=store, workspace=workspace, forge=forge)

    assert (
        published.publication["change_number"] == retried.publication["change_number"]
    )
    assert store.get("T1") == retried
    assert runs.inventory() == before

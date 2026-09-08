"""GitLab's remote boundary, exercised without a network or credentials."""

import json
import subprocess

import pytest

from machinist.forge import ForgeError
from machinist.gitlab import GitLabClient

HOST = "git.example.com"
REPO = "team/subgroup/demo"
SHA = "a" * 40
PROJECT = {
    "id": 123,
    "path_with_namespace": REPO,
    "web_url": f"https://{HOST}/{REPO}",
    "default_branch": "main",
}


def mr(**changes):
    return {
        "iid": 7,
        "source_project_id": 123,
        "target_project_id": 123,
        "web_url": f"https://{HOST}/{REPO}/-/merge_requests/7",
        "source_branch": "machinist/task-1",
        "target_branch": "main",
        "sha": SHA,
        "state": "opened",
        "draft": True,
        **changes,
    }


class Runner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, subprocess.CompletedProcess):
            return response
        output = response if isinstance(response, str) else json.dumps(response)
        return subprocess.CompletedProcess(argv, 0, output, "")


def client(runner):
    return GitLabClient(REPO, host=HOST, runner=runner)


def test_issue_intake_binds_nested_project_and_host():
    runner = Runner(
        PROJECT,
        {
            "iid": 12,
            "project_id": 123,
            "title": "An objective",
            "description": "Acceptance criteria\nwith two lines",
            "web_url": f"https://{HOST}/{REPO}/-/issues/12",
        },
    )
    issue = client(runner).get_issue(12)
    assert (issue.provider, issue.host, issue.repository, issue.number) == (
        "gitlab",
        HOST,
        REPO,
        12,
    )
    assert issue.body == "Acceptance criteria\nwith two lines"
    assert runner.calls[0][0] == [
        "glab",
        "api",
        "projects/team%2Fsubgroup%2Fdemo",
        "--hostname",
        HOST,
        "--method",
        "GET",
    ]
    assert runner.calls[1][0][2].endswith("/issues/12")
    assert all(call[1]["timeout"] == 30 for call in runner.calls)


def test_default_branch_requires_exact_project_identity():
    assert client(Runner(PROJECT)).default_branch() == "main"
    with pytest.raises(ForgeError, match="repository"):
        client(
            Runner({**PROJECT, "path_with_namespace": "other/demo"})
        ).default_branch()


def test_find_change_paginates_and_ignores_forks_and_prefix_matches():
    runner = Runner(
        PROJECT,
        [
            mr(source_project_id=999),
            mr(source_branch="machinist/task-10"),
            mr(state="closed"),
            mr(iid=8, web_url=f"https://{HOST}/{REPO}/-/merge_requests/8"),
        ],
    )
    found = client(runner).find_change("machinist/task-1")
    assert found.number == 8
    assert found.state == "OPEN"
    argv = runner.calls[-1][0]
    assert "--paginate" in argv
    assert "source_branch=machinist%2Ftask-1" in argv[2]
    assert "scope=all" in argv[2]
    assert "state=all" in argv[2]


def test_find_change_accepts_paginated_json_arrays_and_empty_results():
    runner = Runner(PROJECT, "[]\n" + json.dumps([mr()]))
    assert client(runner).find_change("machinist/task-1").number == 7
    assert client(Runner(PROJECT, [])).find_change("none") is None


def test_find_change_rejects_multiple_open_candidates():
    with pytest.raises(ForgeError, match="multiple"):
        client(Runner(PROJECT, [mr(), mr()])).find_change("machinist/task-1")


@pytest.mark.parametrize("payload", ["", [{}], [mr(source_project_id=None)]])
def test_find_does_not_treat_malformed_pagination_as_absent_candidate(payload):
    with pytest.raises(ForgeError):
        client(Runner(PROJECT, payload)).find_change("machinist/task-1")


@pytest.mark.parametrize(
    "changes",
    [
        {"source_project_id": 999},
        {"target_project_id": 999},
        {"draft": "false"},
        {"sha": ""},
        {"source_branch": ""},
        {"target_branch": None},
        {"state": "unknown"},
        {"web_url": "https://attacker.example/a"},
        {"iid": 8},
    ],
)
def test_exact_change_fails_closed_on_malformed_or_mismatched_metadata(changes):
    with pytest.raises(ForgeError):
        client(Runner(PROJECT, mr(**changes))).get_change(7)


def test_create_and_update_send_literal_json_and_normalize_draft_titles():
    title = "[Draft] Draft: Make templates work"
    body = "@/private/secret\n${HOME} :fullpath $(whoami)"
    runner = Runner(PROJECT, mr(), mr(draft=False))
    forge = client(runner)
    created = forge.create_change(
        branch="machinist/task-1", base="main", title=title, body=body
    )
    assert created.is_draft
    forge.update_change(7, title=title, body=body, draft=False)
    posted = json.loads(runner.calls[1][1]["input"])
    assert posted == {
        "source_branch": "machinist/task-1",
        "target_branch": "main",
        "title": "Draft: Make templates work",
        "description": body,
    }
    assert runner.calls[1][0][-2:] == ["--input", "-"]
    updated = json.loads(runner.calls[2][1]["input"])
    assert updated == {"title": "Make templates work", "description": body}
    assert runner.calls[2][0][runner.calls[2][0].index("--method") + 1] == "PUT"


def test_upsert_comment_is_scoped_to_project_and_merge_request():
    runner = Runner(PROJECT, {"id": 77}, {"id": 77})
    forge = client(runner)
    assert forge.upsert_comment(7, "report") == 77
    assert forge.upsert_comment(7, "updated", comment_id=77) == 77
    assert runner.calls[-1][0][2].endswith("/merge_requests/7/notes/77")


def test_upsert_rejects_a_different_returned_note_id():
    with pytest.raises(ForgeError, match="comment"):
        client(Runner(PROJECT, {"id": 78})).upsert_comment(7, "updated", comment_id=77)


@pytest.mark.parametrize(
    "ambient,target,keep",
    [
        (None, "gitlab.com", True),
        (None, HOST, False),
        (HOST, HOST, True),
        ("https://gitlab.com", HOST, False),
        (HOST, "gitlab.com", False),
        (f"https://{HOST}", HOST, True),
    ],
)
@pytest.mark.parametrize("host_variable", ["GITLAB_HOST", "GL_HOST", "GITLAB_URI"])
def test_ambient_tokens_cannot_cross_hosts(
    monkeypatch, ambient, target, keep, host_variable
):
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.delenv("GITLAB_URI", raising=False)
    monkeypatch.delenv("GL_HOST", raising=False)
    if ambient:
        monkeypatch.setenv(host_variable, ambient)
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    monkeypatch.setenv("GITLAB_ACCESS_TOKEN", "test-token-2")
    monkeypatch.setenv("OAUTH_TOKEN", "test-token-3")
    monkeypatch.setenv("GLAB_DEBUG_HTTP", "true")
    monkeypatch.setenv("GLAB_DEBUG", "true")
    monkeypatch.setenv("GLAB_ENABLE_CI_AUTOLOGIN", "true")
    project = {**PROJECT, "web_url": f"https://{target}/{REPO}"}
    runner = Runner(project)
    GitLabClient(REPO, host=target, runner=runner).default_branch()
    environment = runner.calls[0][1]["env"]
    assert ("GITLAB_TOKEN" in environment) is keep
    assert ("GITLAB_ACCESS_TOKEN" in environment) is keep
    assert ("OAUTH_TOKEN" in environment) is keep
    assert "GITLAB_HOST" not in environment
    assert "GL_HOST" not in environment
    assert "GLAB_DEBUG_HTTP" not in environment
    assert "GLAB_DEBUG" not in environment
    assert environment["GLAB_ENABLE_CI_AUTOLOGIN"] == "false"
    assert environment["GLAB_NO_PROMPT"] == "true"
    assert environment["GLAB_SEND_TELEMETRY"] == "false"


@pytest.mark.parametrize(
    "response,match",
    [
        (FileNotFoundError(), "glab auth login"),
        (subprocess.TimeoutExpired("glab", 30), "timed out"),
        (subprocess.CompletedProcess("glab", 1, "", "HTTP 403 Forbidden"), "403"),
        ("not json", "invalid JSON"),
        ([], "object"),
    ],
)
def test_cli_failures_are_actionable_forge_errors(response, match):
    with pytest.raises(ForgeError, match=match):
        client(Runner(response)).default_branch()


def test_glab_failure_diagnostics_are_safe_and_bounded():
    stderr = (
        "\x1b[31mHTTP 403 Forbidden\x1b[0m\n"
        "https://synthetic-user:sentinel-url-secret@example.test/repo\n"
        "Authorization: Bearer sentinel-header-secret\n"
        "GITLAB_TOKEN=sentinel-token-secret\n" + "detail " * 1000
    )
    response = subprocess.CompletedProcess("glab", 1, "", stderr)

    with pytest.raises(ForgeError) as caught:
        client(Runner(response)).default_branch()

    message = str(caught.value)
    assert message.startswith("glab api failed:")
    assert "HTTP 403 Forbidden" in message
    assert "sentinel" not in message
    assert "\x1b" not in message
    assert "truncated" in message
    assert len(message) <= 2000


@pytest.mark.parametrize(
    "repository,host",
    [
        ("team/../demo", HOST),
        ("team/demo?x=y", HOST),
        ("demo", HOST),
        (REPO, "https://gitlab.com"),
        (REPO, "user@gitlab.com"),
    ],
)
def test_invalid_targets_fail_before_invocation(repository, host):
    runner = Runner()
    with pytest.raises(ForgeError):
        GitLabClient(repository, host=host, runner=runner)
    assert not runner.calls

"""GitLab Task intake and MR publication through authenticated glab API calls."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from machinist.diagnostics import sanitize_diagnostic
from machinist.forge import (
    ExternalTask,
    ForgeError,
    PublishedChange,
    normalize_host,
    normalize_repository,
    positive_number,
    validate_ref,
    validate_sha,
    validate_url,
)
from machinist.github import Runner


class GitLabClient:
    provider = "gitlab"

    def __init__(
        self,
        repository: str,
        *,
        host: str = "gitlab.com",
        runner: Runner = subprocess.run,
    ):
        self.host = normalize_host(host)
        self.repository = normalize_repository(repository)
        self._endpoint = "projects/" + quote(self.repository, safe="")
        self._runner = runner
        self._project: dict[str, Any] | None = None

    def get_issue(self, number: int) -> ExternalTask:
        positive_number(number)
        project = self._get_project()
        issue = self._api(f"/issues/{number}")
        if (
            positive_number(issue.get("iid")) != number
            or positive_number(issue.get("project_id")) != project["id"]
        ):
            raise ForgeError("GitLab issue does not match bound repository or Task")
        url = validate_url(
            issue.get("web_url"), self.host, f"/{self.repository}/-/issues/{number}"
        )
        title = self._text(issue, "title")
        body = issue.get("description") or ""
        if not isinstance(body, str):
            raise ForgeError("invalid GitLab issue description")
        return ExternalTask(
            self.provider, self.host, self.repository, number, title, body, url
        )

    def default_branch(self) -> str:
        return validate_ref(self._get_project().get("default_branch"))

    def find_change(self, branch: str) -> PublishedChange | None:
        validate_ref(branch)
        project = self._get_project()
        query = urlencode(
            {
                "scope": "all",
                "state": "all",
                "source_branch": branch,
                "order_by": "updated_at",
                "sort": "desc",
                "per_page": 100,
            }
        )
        items = self._api(f"/merge_requests?{query}", paginate=True)
        matches = []
        for item in items:
            if not isinstance(item, dict):
                raise ForgeError("invalid GitLab merge request list item")
            if validate_ref(item.get("source_branch")) != branch:
                continue
            # A fork can deliberately reuse the controller's candidate branch.
            if positive_number(item.get("source_project_id")) != project["id"]:
                continue
            matches.append(self._change(item))
        opened = [change for change in matches if change.state == "OPEN"]
        if len(opened) > 1:
            raise ForgeError("multiple open merge requests for the candidate branch")
        return opened[0] if opened else next(iter(matches), None)

    def get_change(self, number: int) -> PublishedChange:
        positive_number(number)
        self._get_project()
        change = self._change(self._api(f"/merge_requests/{number}"))
        if change.number != number:
            raise ForgeError("GitLab change number does not match requested change")
        return change

    def create_change(
        self, *, branch: str, base: str, title: str, body: str, draft: bool = True
    ) -> PublishedChange:
        validate_ref(branch)
        validate_ref(base)
        self._get_project()
        item = self._api(
            "/merge_requests",
            method="POST",
            payload={
                "source_branch": branch,
                "target_branch": base,
                "title": _draft_title(title, draft),
                "description": body,
            },
        )
        return self._change(item)

    def update_change(
        self, number: int, *, title: str, body: str, draft: bool
    ) -> PublishedChange:
        positive_number(number)
        self._get_project()
        change = self._change(
            self._api(
                f"/merge_requests/{number}",
                method="PUT",
                payload={
                    "title": _draft_title(title, draft),
                    "description": body,
                },
            )
        )
        if change.number != number:
            raise ForgeError("GitLab change number does not match requested change")
        return change

    def upsert_comment(
        self, number: int, body: str, *, comment_id: int | None = None
    ) -> int:
        positive_number(number)
        self._get_project()
        endpoint = f"/merge_requests/{number}/notes"
        if comment_id is not None:
            endpoint += f"/{positive_number(comment_id)}"
        data = self._api(
            endpoint,
            method="POST" if comment_id is None else "PUT",
            payload={"body": body},
        )
        observed_id = positive_number(data.get("id"))
        if comment_id is not None and observed_id != comment_id:
            raise ForgeError("GitLab comment ID does not match requested comment")
        return observed_id

    def _get_project(self) -> dict[str, Any]:
        if self._project is None:
            project = self._api("")
            positive_number(project.get("id"))
            if (
                normalize_repository(project.get("path_with_namespace"))
                != self.repository
            ):
                raise ForgeError("GitLab project does not match bound repository")
            validate_url(project.get("web_url"), self.host, f"/{self.repository}")
            self._project = project
        return self._project

    def _change(self, item: dict[str, Any]) -> PublishedChange:
        project = self._get_project()
        if any(
            positive_number(item.get(field)) != project["id"]
            for field in ("source_project_id", "target_project_id")
        ):
            raise ForgeError(
                "GitLab change source/target repository does not match controller"
            )
        number = positive_number(item.get("iid"))
        url = validate_url(
            item.get("web_url"),
            self.host,
            f"/{self.repository}/-/merge_requests/{number}",
        )
        draft = item.get("draft")
        states = {"opened": "OPEN", "closed": "CLOSED", "merged": "MERGED"}
        state = item.get("state")
        if not isinstance(state, str) or state not in states or type(draft) is not bool:
            raise ForgeError("invalid GitLab change state or draft metadata")
        return PublishedChange(
            self.provider,
            self.host,
            self.repository,
            number,
            url,
            validate_ref(item.get("source_branch")),
            validate_ref(item.get("target_branch")),
            validate_sha(item.get("sha")),
            states[state],
            draft,
        )

    def _api(
        self,
        suffix: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        paginate: bool = False,
    ) -> Any:
        argv = [
            "glab",
            "api",
            self._endpoint + suffix,
            "--hostname",
            self.host,
            "--method",
            method,
        ]
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 30,
            "env": gitlab_command_environment(self.host),
        }
        if payload is not None:
            # --input prevents glab interpreting @paths and :fullpath in prose.
            argv += ["--input", "-"]
            kwargs["input"] = json.dumps(payload)
        if paginate:
            argv.append("--paginate")
        try:
            result = self._runner(argv, **kwargs)
        except FileNotFoundError as exc:
            raise ForgeError(
                f"glab CLI not found; install it and run 'glab auth login --hostname {self.host}'"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ForgeError("glab api timed out after 30 seconds") from exc
        if result.returncode != 0:
            raise ForgeError(
                sanitize_diagnostic(f"glab api failed: {result.stderr.strip()}")
            )
        try:
            if paginate:
                # Current glab aggregates arrays; older versions emit one array
                # per page. Decode complete JSON values, never partial lines.
                remaining = result.stdout.strip()
                if not remaining:
                    raise ForgeError("glab pagination returned an empty response")
                items: list[Any] = []
                decoder = json.JSONDecoder()
                while remaining:
                    page, end = decoder.raw_decode(remaining)
                    if not isinstance(page, list):
                        raise ForgeError("glab pagination returned a non-array page")
                    items.extend(page)
                    remaining = remaining[end:].strip()
                return items
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ForgeError(f"glab api returned invalid JSON: {exc.msg}") from exc
        if not isinstance(data, dict):
            raise ForgeError("glab api returned a non-object response")
        return data

    @staticmethod
    def _text(item: dict[str, Any], field: str) -> str:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ForgeError(f"invalid GitLab {field}")
        return value


def _draft_title(title: str, draft: bool) -> str:
    if not isinstance(title, str):
        raise ForgeError("invalid merge request title")
    clean = title.strip()
    prefix = re.compile(
        r"^(?:draft:|\[draft\]|\(draft\)|wip:|\[wip\]|\(wip\))\s*", re.IGNORECASE
    )
    while prefix.match(clean):
        clean = prefix.sub("", clean, count=1)
    if not clean:
        raise ForgeError("merge request title must not be empty")
    return ("Draft: " if draft else "") + clean


def gitlab_command_environment(host: str) -> dict[str, str]:
    """Keep ambient credentials on their declared host; otherwise use glab's store."""
    environment = os.environ.copy()
    ambient = (
        environment.get("GITLAB_HOST")
        or environment.get("GL_HOST")
        or environment.get("GITLAB_URI")
    )
    if ambient:
        parsed = urlsplit(ambient if "://" in ambient else f"https://{ambient}")
        ambient = parsed.netloc.casefold()
    if (ambient or "gitlab.com") != host:
        for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"):
            environment.pop(name, None)
    for name in (
        "GITLAB_HOST",
        "GL_HOST",
        "GITLAB_URI",
        "GLAB_REPO",
        "GLAB_DEBUG_HTTP",
        "GLAB_DEBUG",
        "DEBUG",
    ):
        environment.pop(name, None)
    # Never let a local publication silently establish CI credentials/config.
    environment["GLAB_ENABLE_CI_AUTOLOGIN"] = "false"
    environment["GLAB_PROMPT_DISABLED"] = "true"
    environment["GLAB_NO_PROMPT"] = "true"
    environment["GLAB_SEND_TELEMETRY"] = "false"
    return environment

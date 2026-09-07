"""Small remote intake/publication boundary for guided local Tasks.

This does not replace the legacy GitHub pipeline or authorize execution.
Local Approval and Git pushes belong to the controller. Forge publication
only exposes an already verified candidate for human review; it never merges.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlsplit

from machinist.github import (
    _PR_JSON_FIELDS,
    GitHubClient,
    GitHubError,
    PullRequest,
    Runner,
    _pull_request,
)


class ForgeError(Exception):
    """Remote intake or publication could not preserve its contract."""


@dataclass(frozen=True)
class ExternalTask:
    provider: str
    host: str
    repository: str
    number: int
    title: str
    body: str
    url: str


@dataclass(frozen=True)
class PublishedChange:
    provider: str
    host: str
    repository: str
    number: int
    url: str
    branch: str
    base: str
    head_sha: str
    state: str
    is_draft: bool


class ForgeClient(Protocol):
    provider: str
    host: str
    repository: str

    def get_issue(self, number: int) -> ExternalTask: ...

    def default_branch(self) -> str: ...

    def find_change(self, branch: str) -> PublishedChange | None: ...

    def get_change(self, number: int) -> PublishedChange: ...

    def create_change(
        self, *, branch: str, base: str, title: str, body: str, draft: bool = True
    ) -> PublishedChange: ...

    def update_change(
        self, number: int, *, title: str, body: str, draft: bool
    ) -> PublishedChange: ...

    def upsert_comment(
        self, number: int, body: str, *, comment_id: int | None = None
    ) -> int: ...


def publish_change(
    client: ForgeClient,
    *,
    branch: str,
    base: str,
    head_sha: str,
    title: str,
    body: str,
    draft: bool = False,
    expected_number: int | None = None,
) -> PublishedChange:
    """Recover or create one exact candidate, then verify remote delivery.

    The caller must push this head first using its independently checked lease.
    An uncertain API result is recoverable by calling again: the same branch is
    looked up before creation. Closed/merged changes are never silently reopened.
    """
    validate_ref(branch)
    validate_ref(base)
    validate_sha(head_sha)
    if expected_number is not None:
        positive_number(expected_number)

    def verify(change: PublishedChange, *, expected_draft: bool | None = None) -> None:
        expected = {
            "provider": client.provider,
            "host": client.host,
            "repository": client.repository,
            "branch": branch,
            "base": base,
            "head_sha": head_sha,
            "state": "OPEN",
        }
        mismatches = [
            key for key, value in expected.items() if getattr(change, key) != value
        ]
        if expected_draft is not None and change.is_draft is not expected_draft:
            mismatches.append("draft")
        if mismatches:
            raise ForgeError("publication custody mismatch: " + ", ".join(mismatches))

    change = client.find_change(branch)
    if expected_number is not None and (
        change is None or change.number != expected_number
    ):
        raise ForgeError("publication custody mismatch: checkpointed change number")
    if change is None:
        # Never expose an unverified creation as ready for human integration.
        change = client.create_change(
            branch=branch, base=base, title=title, body=body, draft=True
        )
        verify(change, expected_draft=True)
    else:
        verify(change)
    number = change.number
    updated = client.update_change(number, title=title, body=body, draft=draft)
    if updated.number != number:
        raise ForgeError("publication custody mismatch: number")
    verify(updated, expected_draft=draft)
    observed = client.get_change(number)
    if observed.number != number:
        raise ForgeError("publication custody mismatch: number")
    verify(observed, expected_draft=draft)
    return observed


class GitHubForgeClient:
    """Reuse gh authentication and operations without GitHub Approval markers."""

    provider = "github"

    def __init__(
        self,
        repository: str,
        *,
        host: str = "github.com",
        runner: Runner = subprocess.run,
    ):
        self.host = normalize_host(host)
        self.repository = normalize_repository(repository)
        if self.repository.count("/") != 1:
            raise ForgeError("GitHub repository must look like owner/repo")
        self._client = GitHubClient(repo=self.repository, runner=runner)
        self._call(self._client.bind_repository, self.repository, hostname=self.host)

    def get_issue(self, number: int) -> ExternalTask:
        positive_number(number)
        issue = self._call(self._client.get_issue, number)
        if issue.number != number:
            raise ForgeError("issue number does not match requested Task")
        validate_url(issue.url, self.host, f"/{self.repository}/issues/{number}")
        return ExternalTask(
            self.provider,
            self.host,
            self.repository,
            number,
            issue.title,
            issue.body,
            issue.url,
        )

    def default_branch(self) -> str:
        return validate_ref(self._call(self._client.default_branch))

    def find_change(self, branch: str) -> PublishedChange | None:
        validate_ref(branch)
        change = self._call(self._client.pr_for_branch, branch)
        return None if change is None else self._convert(change)

    def get_change(self, number: int) -> PublishedChange:
        positive_number(number)
        item = self._call(
            self._client._gh_json, "pr", "view", str(number), "--json", _PR_JSON_FIELDS
        )
        try:
            change = self._convert(_pull_request(item))
        except (GitHubError, KeyError, TypeError) as exc:
            raise ForgeError(f"invalid GitHub change metadata: {exc}") from exc
        if change.number != number:
            raise ForgeError("change number does not match requested change")
        return change

    def create_change(
        self, *, branch: str, base: str, title: str, body: str, draft: bool = True
    ) -> PublishedChange:
        validate_ref(branch)
        validate_ref(base)
        created = self._call(
            self._client.create_draft_pr,
            branch=branch,
            base=base,
            title=title,
            body=body,
        )
        result = self.get_change(created.number)
        if not draft:
            self._call(self._client.mark_ready, result.number)
            result = self.get_change(result.number)
        return result

    def update_change(
        self, number: int, *, title: str, body: str, draft: bool
    ) -> PublishedChange:
        positive_number(number)
        self._call(self._client.update_pr, number, title=title, body=body)
        change = self.get_change(number)
        if change.is_draft is not draft:
            self._call(
                self._client.mark_draft if draft else self._client.mark_ready, number
            )
            change = self.get_change(number)
        return change

    def upsert_comment(
        self, number: int, body: str, *, comment_id: int | None = None
    ) -> int:
        positive_number(number)
        return self._call(
            self._client.upsert_pr_comment, number, body, comment_id=comment_id
        )

    def _convert(self, pr: PullRequest) -> PublishedChange:
        if pr.is_cross_repository or pr.head_repository != self.repository:
            raise ForgeError("change source repository does not match controller")
        positive_number(pr.number)
        validate_url(pr.url, self.host, f"/{self.repository}/pull/{pr.number}")
        validate_ref(pr.branch)
        validate_ref(pr.base)
        validate_sha(pr.head_sha)
        if (
            pr.state not in {"OPEN", "CLOSED", "MERGED"}
            or type(pr.is_draft) is not bool
        ):
            raise ForgeError("invalid GitHub change state or draft metadata")
        return PublishedChange(
            self.provider,
            self.host,
            self.repository,
            pr.number,
            pr.url,
            pr.branch,
            pr.base,
            pr.head_sha,
            pr.state,
            pr.is_draft,
        )

    @staticmethod
    def _call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except (GitHubError, KeyError, TypeError, ValueError) as exc:
            raise ForgeError(str(exc)) from exc


def normalize_host(value: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?", value
        )
        is None
    ):
        raise ForgeError("forge host must be a hostname with an optional port")
    try:
        _ = urlsplit(f"https://{value}").port
    except ValueError as exc:
        raise ForgeError("forge host has an invalid port") from exc
    return value.casefold()


def normalize_repository(value: object) -> str:
    if not isinstance(value, str):
        raise ForgeError("invalid forge repository")
    candidate = value.removesuffix(".git")
    parts = candidate.split("/")
    if len(parts) < 2 or any(
        part in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part) is None
        for part in parts
    ):
        raise ForgeError(
            "forge repository must be namespace/project, optionally nested"
        )
    return candidate.casefold()


def positive_number(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ForgeError("remote number must be a positive integer")
    return value


def validate_ref(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or character in " ~^:?*[\\" for character in value)
        or value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or any(
            part in {"", ".", ".."} or part.startswith(".") or part.endswith(".lock")
            for part in value.split("/")
        )
        or ".." in value
        or "@{" in value
    ):
        raise ForgeError("invalid change branch or base")
    return value


def validate_sha(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None
    ):
        raise ForgeError("invalid change head SHA")
    return value


def validate_url(value: object, host: str, path: str) -> str:
    if not isinstance(value, str):
        raise ForgeError("invalid remote URL")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ForgeError("invalid remote URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.casefold() != host
        or unquote(parsed.path).casefold() != path.casefold()
        or parsed.query
        or parsed.fragment
    ):
        raise ForgeError("remote URL does not match bound repository identity")
    return value

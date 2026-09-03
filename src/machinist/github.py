"""GitHub operations via the gh CLI.

gh handles auth everywhere machinist runs: the user's keychain locally,
GITHUB_TOKEN inside Actions runners. Wrapping it keeps token management
out of this codebase entirely.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]

# gh list commands default to 30 items.  A solo-developer queue can still
# exceed that (and PR filtering happens after the fetch), so make gh paginate
# well beyond its implicit first page instead of silently hiding eligible work.
_LIST_LIMIT = 1000
_PR_JSON_FIELDS = (
    "number,title,url,headRefName,headRefOid,isDraft,state,labels,"
    "isCrossRepository,headRepository,baseRefName"
)
_MAX_PR_COMMENT_CHARS = 16_000
_COMMENT_TRUNCATION_NOTICE = "\n\n_… truncated by AgentMachinist._"
_COMMAND_TIMEOUT_SECONDS = 30


class GitHubError(Exception):
    """A gh invocation failed."""


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    url: str
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DraftPR:
    number: int
    url: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    url: str
    branch: str
    is_draft: bool
    head_sha: str = ""
    labels: list[str] = field(default_factory=list)
    state: str = "OPEN"
    is_cross_repository: bool = False
    head_repository: str | None = None
    # Kept trailing/defaulted so existing test doubles and API consumers that
    # construct PullRequest directly remain source-compatible. Production gh
    # responses are parsed strictly and always populate this field.
    base: str = ""


class GitHubClient:
    def __init__(self, repo: str | None = None, runner: Runner = subprocess.run):
        self.repo = repo
        self.repo_host: str | None = None
        self._runner = runner

    def bind_repository(self, identity: str, *, hostname: str = "github.com") -> str:
        """Bind all subsequent gh calls to one explicit host and owner/repo."""
        resolved = normalize_repository_identity(identity)
        current = normalize_repository_identity(self.repo)
        normalized_host = _normalize_hostname(hostname)
        if resolved is None or normalized_host is None:
            raise GitHubError("repository identity must look like 'owner/repo'")
        if current is not None and current != resolved:
            raise GitHubError(
                "GitHub client repository does not match controller origin"
            )
        self.repo = resolved
        self.repo_host = normalized_host
        return resolved

    def get_issue(self, number: int) -> Issue:
        data = self._gh_json(
            "issue",
            "view",
            str(number),
            "--json",
            "number,title,body,url,labels",
        )
        return Issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            url=data["url"],
            labels=[label["name"] for label in data.get("labels", [])],
        )

    def create_issue(self, *, title: str, body: str) -> Issue:
        url = self._gh(
            "issue",
            "create",
            "--title",
            title,
            "--body",
            body,
        ).strip()
        number_text = url.rstrip("/").rsplit("/", 1)[-1]
        try:
            number = int(number_text)
        except ValueError as exc:
            raise GitHubError("gh issue create returned an invalid issue URL") from exc
        return Issue(number=number, title=title, body=body, url=url)

    def add_issue_label(self, number: int, label: str) -> None:
        self._gh("issue", "edit", str(number), "--add-label", label)

    def issues_with_label(self, label: str) -> list[Issue]:
        data = self._gh_json(
            "issue",
            "list",
            "--label",
            label,
            "--state",
            "open",
            "--limit",
            str(_LIST_LIMIT),
            "--json",
            "number,title,body,url,labels",
        )
        return [
            Issue(
                number=item["number"],
                title=item["title"],
                body=item.get("body") or "",
                url=item["url"],
                labels=[label["name"] for label in item.get("labels", [])],
            )
            for item in data
        ]

    def open_machinist_prs(self, branch_prefix: str) -> list[PullRequest]:
        data = self._gh_json(
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            str(_LIST_LIMIT),
            "--json",
            _PR_JSON_FIELDS,
        )
        prs = [
            _pull_request(item)
            for item in data
            if item["headRefName"].startswith(branch_prefix)
        ]
        return [pr for pr in prs if self._same_repository_pr(pr)]

    def pr_for_branch(self, branch: str) -> PullRequest | None:
        """Return the exact branch's open or most-recent historical PR."""

        data = self._gh_json(
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            branch,
            "--limit",
            str(_LIST_LIMIT),
            "--json",
            _PR_JSON_FIELDS,
        )
        matches = [
            pr
            for item in data
            if item["headRefName"] == branch
            for pr in (_pull_request(item),)
            if self._same_repository_pr(pr)
        ]
        if not matches:
            return None
        return next((pr for pr in matches if pr.state == "OPEN"), matches[0])

    def default_branch(self) -> str:
        data = self._gh_json("repo", "view", "--json", "defaultBranchRef")
        return data["defaultBranchRef"]["name"]

    def create_draft_pr(
        self, *, branch: str, base: str, title: str, body: str
    ) -> DraftPR:
        url = self._gh(
            "pr",
            "create",
            "--draft",
            "--head",
            branch,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ).strip()
        return DraftPR(number=int(url.rstrip("/").rsplit("/", 1)[-1]), url=url)

    def mark_ready(self, number: int) -> None:
        self._gh("pr", "ready", str(number))

    def mark_draft(self, number: int) -> None:
        self._gh("pr", "ready", str(number), "--undo")

    def reopen_pr(self, number: int) -> None:
        self._gh("pr", "reopen", str(number))

    def update_pr(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
    ) -> None:
        if title is None and body is None:
            raise ValueError("update_pr requires a title or body")
        args = ["pr", "edit", str(number)]
        if title is not None:
            args.extend(["--title", title])
        if body is not None:
            args.extend(["--body", body])
        self._gh(*args)

    def close_pr(self, number: int) -> None:
        self._gh("pr", "close", str(number))

    def remove_pr_label(self, number: int, label: str) -> None:
        self._gh("pr", "edit", str(number), "--remove-label", label)

    def remove_issue_label(self, number: int, label: str) -> None:
        self._gh("issue", "edit", str(number), "--remove-label", label)

    def upsert_pr_comment(
        self,
        number: int,
        body: str,
        *,
        comment_id: int | None = None,
    ) -> int:
        """Post a bounded report comment, or update its checkpointed ID."""

        bounded = _bounded_comment(body)
        repo = self.repo or "{owner}/{repo}"
        if comment_id is None:
            endpoint = f"repos/{repo}/issues/{number}/comments"
            method = "POST"
        else:
            if comment_id <= 0:
                raise ValueError("comment_id must be positive")
            endpoint = f"repos/{repo}/issues/comments/{comment_id}"
            method = "PATCH"
        data = self._gh_api_json(
            "--method",
            method,
            endpoint,
            "-f",
            f"body={bounded}",
        )
        return int(data["id"])

    def approval_sha(self, number: int) -> str | None:
        """Return the newest SHA-bound approval recorded on a PR."""
        data = self._gh_json("pr", "view", str(number), "--json", "comments")
        marker = re.compile(
            r"<!--\s*agentmachinist:approval\s+sha=([0-9a-fA-F]{40})\s*-->"
        )
        for comment in reversed(data.get("comments", [])):
            # Only the managed approve workflow mints Evidence, after checking
            # the actor's write access. A marker typed by a human is not
            # Approval, whatever their repository association.
            login = (comment.get("author") or {}).get("login")
            if login not in {"github-actions", "github-actions[bot]"}:
                continue
            match = marker.search(comment.get("body") or "")
            if match:
                return match.group(1).lower()
        return None

    def approve_pr(self, number: int, *, head_sha: str) -> None:
        """Request server-side Approval for exactly one observed PR head.

        The managed workflow re-reads the current PR head before it records
        Evidence or adds the approval label. Keeping both mutations in that
        server-side transaction prevents a branch update between a local
        comment and a local label write from authorizing the wrong commit.
        """
        self._gh(
            "pr",
            "comment",
            str(number),
            "--body",
            f"/machinist-execute {head_sha.lower()}",
        )

    def ensure_label(self, name: str, *, color: str, description: str) -> None:
        # --force updates an existing label instead of failing, making this idempotent.
        self._gh(
            "label",
            "create",
            name,
            "--force",
            "--color",
            color,
            "--description",
            description,
        )

    def label_names(self) -> set[str]:
        data = self._gh_json(
            "label",
            "list",
            "--limit",
            str(_LIST_LIMIT),
            "--json",
            "name",
        )
        try:
            return {item["name"] for item in data}
        except (KeyError, TypeError) as exc:
            raise GitHubError(f"gh label list returned invalid data: {exc}") from exc

    def _gh(self, *args: str) -> str:
        argv = ["gh", *args]
        if self.repo is not None:
            repo_target = self.repo
            if self.repo_host not in (None, "github.com"):
                repo_target = f"{self.repo_host}/{self.repo}"
            # `gh repo view` takes its repository as a positional argument;
            # unlike issue/pr/label commands, it has no --repo flag.
            if args[:2] == ("repo", "view"):
                argv.insert(3, repo_target)
            else:
                argv += ["--repo", repo_target]
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                env=self._command_environment(),
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install it (https://cli.github.com) and run 'gh auth login'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(
                f"gh {' '.join(args[:2])} timed out after {_COMMAND_TIMEOUT_SECONDS} seconds"
            ) from exc
        if result.returncode != 0:
            raise GitHubError(
                f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}"
            )
        return result.stdout

    def _gh_json(self, *args: str) -> Any:
        try:
            return json.loads(self._gh(*args))
        except json.JSONDecodeError as exc:
            raise GitHubError(
                f"gh {' '.join(args[:2])} returned invalid JSON: {exc.msg}"
            ) from exc

    def _gh_api(self, *args: str) -> str:
        argv = ["gh", "api", *args]
        if self.repo_host not in (None, "github.com"):
            argv += ["--hostname", self.repo_host]
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                env=self._command_environment(),
            )
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install it (https://cli.github.com) and run 'gh auth login'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(
                f"gh api timed out after {_COMMAND_TIMEOUT_SECONDS} seconds"
            ) from exc
        if result.returncode != 0:
            raise GitHubError(f"gh api failed: {result.stderr.strip()}")
        return result.stdout

    def _gh_api_json(self, *args: str) -> Any:
        try:
            return json.loads(self._gh_api(*args))
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh api returned invalid JSON: {exc.msg}") from exc

    def _command_environment(self) -> dict[str, str]:
        return github_command_environment(self.repo_host)

    def _same_repository_pr(self, pr: PullRequest) -> bool:
        if pr.is_cross_repository:
            return False
        expected = normalize_repository_identity(self.repo)
        return not (
            expected is not None
            and pr.head_repository is not None
            and pr.head_repository != expected
        )


def github_command_environment(hostname: str | None = None) -> dict[str, str]:
    """Return a gh environment with routing and token authority bound."""
    environment = os.environ.copy()
    ambient_host = _normalize_hostname(environment.get("GH_HOST"))
    environment.pop("GH_REPO", None)
    environment.pop("GH_HOST", None)
    target_host = _normalize_hostname(hostname) or "github.com"
    if target_host == "github.com":
        environment.pop("GH_ENTERPRISE_TOKEN", None)
        environment.pop("GITHUB_ENTERPRISE_TOKEN", None)
    else:
        # GitHub.com tokens are not valid authority for a GHES target.  A
        # generic enterprise token is accepted only when its accompanying
        # GH_HOST named this exact server; otherwise gh must use its
        # hostname-keyed credential store.
        environment.pop("GH_TOKEN", None)
        environment.pop("GITHUB_TOKEN", None)
        if ambient_host != target_host:
            environment.pop("GH_ENTERPRISE_TOKEN", None)
            environment.pop("GITHUB_ENTERPRISE_TOKEN", None)
    return environment


def _pull_request(item: dict[str, Any]) -> PullRequest:
    cross_repository = item.get("isCrossRepository")
    if not isinstance(cross_repository, bool):
        raise GitHubError(
            "gh pr returned invalid isCrossRepository metadata; refusing PR custody"
        )
    head_repository = _head_repository_identity(item.get("headRepository"))
    if head_repository is None:
        raise GitHubError(
            "gh pr returned invalid headRepository metadata; refusing PR custody"
        )
    base = item.get("baseRefName")
    if not isinstance(base, str) or not base:
        raise GitHubError(
            "gh pr returned invalid baseRefName metadata; refusing PR custody"
        )
    return PullRequest(
        number=item["number"],
        title=item["title"],
        url=item["url"],
        branch=item["headRefName"],
        head_sha=item["headRefOid"],
        is_draft=item["isDraft"],
        state=item.get("state", "OPEN"),
        labels=[label["name"] for label in item.get("labels", [])],
        is_cross_repository=cross_repository,
        head_repository=head_repository,
        base=base,
    )


def _head_repository_identity(value: Any) -> str | None:
    if isinstance(value, str):
        return normalize_repository_identity(value)
    if not isinstance(value, dict):
        return None
    direct = value.get("nameWithOwner")
    if isinstance(direct, str):
        return normalize_repository_identity(direct)
    name = value.get("name")
    owner = value.get("owner")
    login = owner.get("login") if isinstance(owner, dict) else None
    if isinstance(login, str) and isinstance(name, str):
        return normalize_repository_identity(f"{login}/{name}")
    return None


def normalize_repository_identity(value: str | None) -> str | None:
    """Return a comparison-safe GitHub ``owner/repo`` identity."""
    if not isinstance(value, str):
        return None
    if value != value.strip() or value.startswith("/") or value.endswith("/"):
        return None
    candidate = value
    if candidate.casefold().endswith(".git"):
        candidate = candidate[:-4]
    if candidate.count("/") != 1 or any(
        character in candidate for character in ("\0", "\n", "\r")
    ):
        return None
    owner, repository = candidate.split("/", 1)
    if (
        not owner
        or not repository
        or owner in {".", ".."}
        or repository in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9_.-]+", owner) is None
        or re.fullmatch(r"[A-Za-z0-9_.-]+", repository) is None
    ):
        return None
    return f"{owner}/{repository}".casefold()


def _normalize_hostname(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if (
        not candidate
        or "/" in candidate
        or "@" in candidate
        or any(character in candidate for character in ("\0", "\n", "\r"))
    ):
        return None
    return candidate


def _bounded_comment(body: str) -> str:
    if len(body) <= _MAX_PR_COMMENT_CHARS:
        return body
    keep = _MAX_PR_COMMENT_CHARS - len(_COMMENT_TRUNCATION_NOTICE)
    return body[:keep] + _COMMENT_TRUNCATION_NOTICE

"""GitHub operations via the gh CLI.

gh handles auth everywhere machinist runs: the user's keychain locally,
GITHUB_TOKEN inside Actions runners. Wrapping it keeps token management
out of this codebase entirely.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]

# gh list commands default to 30 items.  A solo-developer queue can still
# exceed that (and PR filtering happens after the fetch), so make gh paginate
# well beyond its implicit first page instead of silently hiding eligible work.
_LIST_LIMIT = 1000
_PR_JSON_FIELDS = "number,title,url,headRefName,headRefOid,isDraft,state,labels"
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


class GitHubClient:
    def __init__(self, repo: str | None = None, runner: Runner = subprocess.run):
        self.repo = repo
        self._runner = runner

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
        return [
            _pull_request(item)
            for item in data
            if item["headRefName"].startswith(branch_prefix)
        ]

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
            _pull_request(item) for item in data if item["headRefName"] == branch
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
            association = comment.get("authorAssociation")
            login = (comment.get("author") or {}).get("login")
            trusted_author = association in {"OWNER", "MEMBER", "COLLABORATOR"}
            trusted_workflow = login in {"github-actions", "github-actions[bot]"}
            if not (trusted_author or trusted_workflow):
                continue
            match = marker.search(comment.get("body") or "")
            if match:
                return match.group(1).lower()
        return None

    def approve_pr(self, number: int, *, label: str, head_sha: str) -> None:
        """Record immutable approval evidence before exposing the approval label."""
        self._gh(
            "pr",
            "comment",
            str(number),
            "--body",
            f"<!-- agentmachinist:approval sha={head_sha.lower()} -->",
        )
        self._gh("pr", "edit", str(number), "--add-label", label)

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

    def _gh(self, *args: str) -> str:
        argv = ["gh", *args]
        if self.repo is not None:
            argv += ["--repo", self.repo]
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
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
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
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


def _pull_request(item: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=item["number"],
        title=item["title"],
        url=item["url"],
        branch=item["headRefName"],
        head_sha=item["headRefOid"],
        is_draft=item["isDraft"],
        state=item.get("state", "OPEN"),
        labels=[label["name"] for label in item.get("labels", [])],
    )


def _bounded_comment(body: str) -> str:
    if len(body) <= _MAX_PR_COMMENT_CHARS:
        return body
    keep = _MAX_PR_COMMENT_CHARS - len(_COMMENT_TRUNCATION_NOTICE)
    return body[:keep] + _COMMENT_TRUNCATION_NOTICE

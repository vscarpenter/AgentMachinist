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


class GitHubClient:
    def __init__(self, repo: str | None = None, runner: Runner = subprocess.run):
        self.repo = repo
        self._runner = runner

    def get_issue(self, number: int) -> Issue:
        data = self._gh_json(
            "issue", "view", str(number),
            "--json", "number,title,body,url,labels",
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
            "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,url,labels",
        )
        return [
            Issue(
                number=item["number"],
                title=item["title"],
                body=item.get("body") or "",
                url=item["url"],
                labels=[l["name"] for l in item.get("labels", [])],
            )
            for item in data
        ]

    def open_machinist_prs(self, branch_prefix: str) -> list[PullRequest]:
        data = self._gh_json(
            "pr", "list",
            "--state", "open",
            "--json", "number,title,url,headRefName,headRefOid,isDraft,labels",
        )
        return [
            PullRequest(
                number=item["number"],
                title=item["title"],
                url=item["url"],
                branch=item["headRefName"],
                head_sha=item["headRefOid"],
                is_draft=item["isDraft"],
                labels=[l["name"] for l in item.get("labels", [])],
            )
            for item in data
            if item["headRefName"].startswith(branch_prefix)
        ]

    def default_branch(self) -> str:
        data = self._gh_json("repo", "view", "--json", "defaultBranchRef")
        return data["defaultBranchRef"]["name"]

    def create_draft_pr(self, *, branch: str, base: str, title: str, body: str) -> DraftPR:
        url = self._gh(
            "pr", "create", "--draft",
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", body,
        ).strip()
        return DraftPR(number=int(url.rstrip("/").rsplit("/", 1)[-1]), url=url)

    def mark_ready(self, number: int) -> None:
        self._gh("pr", "ready", str(number))

    def approval_sha(self, number: int) -> str | None:
        """Return the newest SHA-bound approval recorded on a PR."""
        data = self._gh_json("pr", "view", str(number), "--json", "comments")
        marker = re.compile(
            r"<!--\s*agentmachinist:approval\s+sha=([0-9a-fA-F]{40})\s*-->"
        )
        for comment in reversed(data.get("comments", [])):
            match = marker.search(comment.get("body") or "")
            if match:
                return match.group(1).lower()
        return None

    def approve_pr(self, number: int, *, label: str, head_sha: str) -> None:
        """Record immutable approval evidence before exposing the approval label."""
        self._gh(
            "pr", "comment", str(number),
            "--body", f"<!-- agentmachinist:approval sha={head_sha.lower()} -->",
        )
        self._gh("pr", "edit", str(number), "--add-label", label)

    def ensure_label(self, name: str, *, color: str, description: str) -> None:
        # --force updates an existing label instead of failing, making this idempotent.
        self._gh(
            "label", "create", name,
            "--force",
            "--color", color,
            "--description", description,
        )

    def _gh(self, *args: str) -> str:
        argv = ["gh", *args]
        if self.repo is not None:
            argv += ["--repo", self.repo]
        try:
            result = self._runner(argv, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise GitHubError(
                "gh CLI not found. Install it (https://cli.github.com) and run 'gh auth login'."
            ) from exc
        if result.returncode != 0:
            raise GitHubError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
        return result.stdout

    def _gh_json(self, *args: str) -> Any:
        try:
            return json.loads(self._gh(*args))
        except json.JSONDecodeError as exc:
            raise GitHubError(
                f"gh {' '.join(args[:2])} returned invalid JSON: {exc.msg}"
            ) from exc

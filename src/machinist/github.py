"""GitHub operations via the gh CLI.

gh handles auth everywhere machinist runs: the user's keychain locally,
GITHUB_TOKEN inside Actions runners. Wrapping it keeps token management
out of this codebase entirely.
"""

from __future__ import annotations

import json
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
        return json.loads(self._gh(*args))

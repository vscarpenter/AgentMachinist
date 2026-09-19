"""Trusted GitHub delegation intake and exact-candidate delivery observation.

All transports go through GitHubClient, which owns gh invocation and credential
routing. Queue content is a snapshot at admission, not an assertion that the
label actor personally wrote or approved every word of the issue.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from machinist.diagnostics import sanitize_diagnostic
from machinist.github import (
    GitHubClient,
    GitHubError,
    PullRequest,
    normalize_repository_identity,
)

CIStatus = Literal["pending", "passed", "failed", "unknown"]


@dataclass(frozen=True)
class QueuedIssue:
    number: int
    title: str
    body: str
    url: str
    event_id: str
    actor: str
    queued_at: str


@dataclass(frozen=True)
class CIObservation:
    status: CIStatus
    summary: str


class BackgroundGitHubClient(GitHubClient):
    """Bound-repository adapter for the opt-in single-worker pilot."""

    def queued_tasks(self, label: str) -> tuple[QueuedIssue, ...]:
        """Freeze open issues whose latest queue event has a trusted actor.

        Read permissions from GitHub itself; author association and the ability
        to apply labels do not establish write authority. A changing issue or
        queue event aborts intake so the next poll obtains a fresh snapshot.
        Transport failures remain visible instead of pretending the queue is empty.
        """
        repo, _ = self._bound_target()
        if not label or any(c in label for c in "\0\n\r"):
            raise GitHubError("invalid background queue label")
        pages = self._gh_api_json(
            "--method",
            "GET",
            f"repos/{repo}/issues",
            "-f",
            "state=open",
            "-f",
            f"labels={label}",
            "-f",
            "per_page=100",
            "--paginate",
            "--slurp",
        )
        admitted: list[QueuedIssue] = []
        seen: set[int] = set()
        for raw in _array_pages(pages):
            if "pull_request" in raw:
                continue
            snapshot = self._issue_snapshot(raw)
            number = snapshot[0]
            if number in seen or not _is_queued(raw, label):
                continue
            seen.add(number)
            event = self._queue_event(number, label)
            if event is None:
                continue
            event_id, actor, queued_at = event
            permission = self._gh_api_json(
                f"repos/{repo}/collaborators/{actor}/permission"
            )
            if not isinstance(permission, dict):
                raise GitHubError("GitHub returned invalid collaborator permission")
            if permission.get("permission") not in {"write", "admin"}:
                continue
            observed_user = permission.get("user")
            if isinstance(observed_user, dict) and observed_user.get("login") != actor:
                raise GitHubError("GitHub collaborator permission identity mismatch")
            current = self._gh_api_json(f"repos/{repo}/issues/{number}")
            current_snapshot = self._issue_snapshot(current)
            if not _is_queued(current, label):
                continue
            if (
                current_snapshot != snapshot
                or self._queue_event(number, label) != event
            ):
                raise GitHubError(
                    f"Issue #{number} changed during queue intake; poll again"
                )
            admitted.append(QueuedIssue(*snapshot, event_id, actor, queued_at))
        return tuple(sorted(admitted, key=lambda item: (item.queued_at, item.number)))

    def observe_ci(self, sha: str, required_checks: tuple[str, ...]) -> CIObservation:
        """Observe explicitly configured checks/status contexts for one SHA.

        A successful optional check cannot substitute for an absent required
        check. Skipped/neutral checks are unknown, not verified success. Names
        are not app identities: repository policy must trust the CI writers.
        """
        repo, _ = self._bound_target()
        _exact_sha(sha)
        if not required_checks:
            return CIObservation("unknown", "No required CI checks are configured.")
        if any(
            not isinstance(name, str) or not name.strip() for name in required_checks
        ):
            raise GitHubError("required CI check names must be nonempty strings")
        checks = self._gh_api_json(
            f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100",
            "--paginate",
            "--slurp",
        )
        statuses = self._gh_api_json(
            f"repos/{repo}/commits/{sha}/status?per_page=100",
            "--paginate",
            "--slurp",
        )
        observations: dict[str, list[CIStatus]] = {}
        try:
            for page in _object_pages(checks):
                for run in _objects(page["check_runs"]):
                    if run.get("head_sha") != sha:
                        return CIObservation(
                            "unknown", "A check response has a different candidate SHA."
                        )
                    name = _name(run.get("name"))
                    observations.setdefault(name, []).append(_check_status(run))
            # The combined status endpoint supplies the exact SHA on every page.
            # Commit statuses are newest first; IDs order repeated contexts even
            # when a transport/test fixture presents pages in a different order.
            latest: dict[str, dict[str, Any]] = {}
            for page in _object_pages(statuses):
                if page.get("sha") != sha:
                    return CIObservation(
                        "unknown", "A status response has a different candidate SHA."
                    )
                for status in _objects(page["statuses"]):
                    name = _name(status.get("context"))
                    identifier = _positive_id(status.get("id"))
                    if name not in latest or identifier > latest[name]["id"]:
                        latest[name] = status
            for name, status in latest.items():
                observations.setdefault(name, []).append(
                    _commit_status(status.get("state"))
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("GitHub returned malformed CI observation data") from exc
        results = {
            name: _aggregate(observations.get(name, ["unknown"]))
            for name in dict.fromkeys(required_checks)
        }
        outcome = _aggregate(list(results.values()))
        summary = "; ".join(f"{name}: {result}" for name, result in results.items())
        return CIObservation(outcome, sanitize_diagnostic(summary))

    def request_ready(
        self,
        pr_number: int,
        expected_sha: str,
        *,
        expected_branch: str | None = None,
        expected_base: str | None = None,
        cancel_check: Callable[[], bool | None] | None = None,
    ) -> None:
        """Request readiness only for the expected candidate; never merge.

        GitHub's ready mutation has no compare-and-swap SHA argument. Recheck
        after the transition and restore draft if the candidate changed. This
        detects an observed race; branch policy still controls subsequent pushes.
        """
        self._bound_target()
        _exact_sha(expected_sha)
        _positive_id(pr_number)
        observed = self.get_pr(pr_number)
        self._ready_custody(
            observed, pr_number, expected_sha, expected_branch, expected_base
        )
        if cancel_check is not None and cancel_check():
            raise GitHubError("Background Task cancelled before PR readiness")
        if not observed.is_draft:
            return
        self.mark_ready(pr_number)
        try:
            after = self.get_pr(pr_number)
            self._ready_custody(
                after, pr_number, expected_sha, expected_branch, expected_base
            )
            if after.is_draft:
                raise GitHubError("PR custody: readiness transition was not observed")
        except GitHubError:
            # Restoring draft is a protective rollback, including if the caller
            # has cancelled since readiness was requested.
            self.mark_draft(pr_number)
            raise

    def open_bot_pr_count(self, branch_prefix: str) -> int:
        """Count same-repository open PRs under the controller-owned prefix."""
        repo, _ = self._bound_target()
        if not branch_prefix:
            raise GitHubError("background PR branch prefix must be nonempty")
        pages = self._gh_api_json(
            f"repos/{repo}/pulls?state=open&per_page=100", "--paginate", "--slurp"
        )
        numbers: set[int] = set()
        for pr in _array_pages(pages):
            try:
                number = _positive_id(pr["number"])
                self._resource_url(pr["html_url"], f"pull/{number}")
                head, base = pr["head"], pr["base"]
                if normalize_repository_identity(base["repo"]["full_name"]) != repo:
                    raise GitHubError("GitHub PR base repository identity mismatch")
                branch = _name(head["ref"])
                if not branch.startswith(branch_prefix) or pr["state"] != "open":
                    continue
                identity = normalize_repository_identity(head["repo"]["full_name"])
                if identity is None:
                    raise GitHubError("GitHub PR head repository identity is missing")
                if identity == repo:
                    numbers.add(number)
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError("GitHub returned malformed open PR metadata") from exc
        return len(numbers)

    def _bound_target(self) -> tuple[str, str]:
        if self.repo is None or self.repo_host is None:
            raise GitHubError(
                "background GitHub operations require a bound repository and host"
            )
        return self.repo, self.repo_host

    def _issue_snapshot(self, value: Any) -> tuple[int, str, str, str]:
        if not isinstance(value, dict):
            raise GitHubError("GitHub returned an invalid issue snapshot")
        try:
            number = _positive_id(value.get("number"))
            title = _name(value.get("title"))
        except (ValueError, TypeError) as exc:
            raise GitHubError("GitHub returned an invalid issue snapshot") from exc
        body = value.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise GitHubError("GitHub returned an invalid issue body")
        url = self._resource_url(value.get("html_url"), f"issues/{number}")
        return number, title, body, url

    def _resource_url(self, value: Any, suffix: str) -> str:
        repo, host = self._bound_target()
        if not isinstance(value, str):
            raise GitHubError("GitHub resource identity is missing")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise GitHubError("GitHub resource identity URL is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != host
            or parsed.path.casefold() != f"/{repo}/{suffix}".casefold()
            or parsed.query
            or parsed.fragment
        ):
            raise GitHubError(
                "GitHub resource identity does not match the bound repository"
            )
        return value

    def _queue_event(self, number: int, label: str) -> tuple[str, str, str] | None:
        repo, _ = self._bound_target()
        pages = self._gh_api_json(
            f"repos/{repo}/issues/{number}/events?per_page=100",
            "--paginate",
            "--slurp",
        )
        events: dict[int, dict[str, Any]] = {}
        for value in _array_pages(pages):
            if value.get("event") not in {"labeled", "unlabeled"}:
                continue
            event_label = value.get("label")
            if not isinstance(event_label, dict) or event_label.get("name") != label:
                continue
            try:
                identifier = _positive_id(value.get("id"))
                timestamp = value["created_at"]
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("missing event timezone")
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise GitHubError(
                    "GitHub returned an invalid queue event identity"
                ) from exc
            if identifier in events and events[identifier] != value:
                raise GitHubError("GitHub returned conflicting queue event identity")
            events[identifier] = value
        if not events:
            return None
        latest = max(events.values(), key=lambda item: item["id"])
        if latest["event"] != "labeled":
            return None
        actor_data = latest.get("actor")
        actor = actor_data.get("login") if isinstance(actor_data, dict) else None
        if (
            not isinstance(actor, str)
            or re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", actor) is None
        ):
            raise GitHubError("GitHub returned an invalid queue actor identity")
        return str(latest["id"]), actor, latest["created_at"]

    def _ready_custody(
        self,
        pr: PullRequest,
        number: int,
        sha: str,
        branch: str | None,
        base: str | None,
    ) -> None:
        repo, _ = self._bound_target()
        try:
            self._resource_url(pr.url, f"pull/{number}")
        except GitHubError as exc:
            raise GitHubError("PR custody: repository URL mismatch") from exc
        if (
            pr.number != number
            or pr.head_sha != sha
            or pr.state != "OPEN"
            or not isinstance(pr.is_draft, bool)
            or pr.is_cross_repository
            or pr.head_repository != repo
            or (branch is not None and pr.branch != branch)
            or (base is not None and pr.base != base)
        ):
            raise GitHubError(
                "PR custody: expected exact open same-repository candidate"
            )


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise GitHubError("GitHub returned malformed paginated data")
    return value


def _object_pages(value: Any) -> list[dict[str, Any]]:
    return _objects(value)


def _array_pages(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise GitHubError("GitHub returned malformed paginated data")
    return [item for page in value for item in _objects(page)]


def _is_queued(value: dict[str, Any], label: str) -> bool:
    labels = _objects(value.get("labels"))
    return value.get("state") == "open" and any(
        item.get("name") == label for item in labels
    )


def _positive_id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("expected positive integer identity")
    return value


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected nonempty name")
    return value


def _exact_sha(value: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise GitHubError("background CI requires an exact canonical commit SHA")


def _check_status(run: dict[str, Any]) -> CIStatus:
    if run.get("status") in {
        "queued",
        "in_progress",
        "pending",
        "waiting",
        "requested",
    }:
        return "pending"
    if run.get("status") != "completed":
        return "unknown"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "passed"
    if conclusion in {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
    }:
        return "failed"
    return "unknown"


def _commit_status(value: Any) -> CIStatus:
    if value == "success":
        return "passed"
    if value == "pending":
        return "pending"
    if value in {"failure", "error"}:
        return "failed"
    return "unknown"


def _aggregate(values: list[CIStatus]) -> CIStatus:
    precedence: tuple[CIStatus, ...] = ("failed", "unknown", "pending")
    for status in precedence:
        if status in values:
            return status
    return "passed"

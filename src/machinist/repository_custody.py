"""Exact controller repository binding and pull-request custody checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from machinist.config import MachinistConfig
from machinist.github import PullRequest, normalize_repository_identity


class RepositoryCustodyError(Exception):
    """The controller cannot prove one exact repository or PR identity."""

    def __init__(self, message: str, *, reasons: frozenset[str] = frozenset()):
        super().__init__(message)
        self.reasons = reasons


class RepositoryClient(Protocol):
    repo: str | None
    repo_host: str | None


class RepositoryWorkspace(Protocol):
    def repository_target(self) -> tuple[str, str]: ...


@dataclass(frozen=True)
class RepositoryTarget:
    host: str
    identity: str


@dataclass(frozen=True)
class PullRequestExpectation:
    number: int
    branch: str
    base: str
    head_sha: str
    repository: str | None
    is_draft: bool | None = None
    state: str = "OPEN"


def bind_repository(
    config: MachinistConfig,
    github: RepositoryClient,
    workspace: RepositoryWorkspace,
) -> RepositoryTarget:
    """Bind GitHub operations to the Workshop controller origin."""
    resolver = getattr(workspace, "repository_target", None)
    if not callable(resolver):
        raise RepositoryCustodyError(
            "cannot prove controller Git origin repository identity"
        )
    try:
        origin_host, raw_origin_identity = resolver()
        origin_identity = normalize_repository_identity(raw_origin_identity)
    except Exception as exc:
        raise RepositoryCustodyError(
            "cannot prove controller Git origin repository identity"
        ) from exc
    if not isinstance(origin_host, str) or not origin_host:
        raise RepositoryCustodyError(
            "cannot prove controller Git origin repository host"
        )

    configured = normalize_repository_identity(config.github.repo)
    if (
        (config.github.repo is not None and configured is None)
        or origin_identity is None
        or (configured is not None and configured != origin_identity)
    ):
        raise RepositoryCustodyError(
            "controller Git origin does not match configured GitHub repository"
        )
    _validate_client_target(github, origin_host, origin_identity)
    _bind_client(github, origin_host, origin_identity)
    return RepositoryTarget(origin_host, origin_identity)


def validate_pr_base(value: str, *, source: str) -> str:
    """Return one safe PR base name or fail before a GitHub mutation."""
    if (
        not value
        or value != value.strip()
        or any(character in value for character in ("\0", "\n", "\r"))
    ):
        raise RepositoryCustodyError(f"{source} returned an invalid PR base")
    return value


def same_repository_pr(
    pr: PullRequest,
    expected_repository: str | None,
) -> bool:
    """Return whether a PR head belongs to the controller repository."""
    if pr.is_cross_repository:
        return False
    expected = normalize_repository_identity(expected_repository)
    observed = normalize_repository_identity(pr.head_repository)
    return expected is None or observed is None or observed == expected


def verify_pull_request(
    observed: PullRequest | None,
    expected: PullRequestExpectation,
) -> PullRequest:
    """Require one exact open PR at the expected branch, base, head, and origin."""
    if observed is None:
        raise RepositoryCustodyError(
            f"GitHub did not observe PR #{expected.number} for branch "
            f"{expected.branch!r}",
            reasons=frozenset({"missing"}),
        )
    mismatches = _identity_mismatches(observed, expected)
    mismatches.extend(_repository_mismatches(observed, expected))
    if expected.is_draft is not None and observed.is_draft is not expected.is_draft:
        expected_draft = "a draft" if expected.is_draft else "ready"
        mismatches.append(("draft", f"PR is not {expected_draft}"))
    if observed.head_sha != expected.head_sha:
        mismatches.append(
            (
                "head",
                f"head {(observed.head_sha or 'missing')[:12]} != "
                f"{expected.head_sha[:12]}",
            )
        )
    if mismatches:
        raise RepositoryCustodyError(
            "; ".join(message for _, message in mismatches),
            reasons=frozenset(reason for reason, _ in mismatches),
        )
    return observed


def _identity_mismatches(
    observed: PullRequest,
    expected: PullRequestExpectation,
) -> list[tuple[str, str]]:
    mismatches: list[tuple[str, str]] = []
    if observed.number != expected.number:
        mismatches.append(
            ("number", f"number #{observed.number} != #{expected.number}")
        )
    if observed.branch != expected.branch:
        mismatches.append(
            ("branch", f"branch {observed.branch!r} != {expected.branch!r}")
        )
    if observed.base and observed.base != expected.base:
        mismatches.append(("base", f"base {observed.base!r} != {expected.base!r}"))
    if observed.state != expected.state:
        mismatches.append(("state", f"state {observed.state!r} != {expected.state!r}"))
    return mismatches


def _repository_mismatches(
    observed: PullRequest,
    expected: PullRequestExpectation,
) -> list[tuple[str, str]]:
    if observed.is_cross_repository:
        return [("repository", "PR is cross-repository")]
    if not same_repository_pr(observed, expected.repository):
        return [("repository", "PR head repository does not match controller origin")]
    return []


def _validate_client_target(github: RepositoryClient, host: str, identity: str) -> None:
    client_target = normalize_repository_identity(getattr(github, "repo", None))
    if client_target is not None and client_target != identity:
        raise RepositoryCustodyError(
            "configured GitHub repository does not match the GitHub client target"
        )
    client_host = getattr(github, "repo_host", None)
    if client_host is not None and str(client_host).casefold() != host.casefold():
        raise RepositoryCustodyError(
            "GitHub client host does not match controller Git origin host"
        )


def _bind_client(github: RepositoryClient, host: str, identity: str) -> None:
    binder = getattr(github, "bind_repository", None)
    try:
        if callable(binder):
            binder(identity, hostname=host)
        else:
            github.repo = identity
            github.repo_host = host
    except Exception as exc:
        raise RepositoryCustodyError(
            "could not bind GitHub client to controller origin repository"
        ) from exc

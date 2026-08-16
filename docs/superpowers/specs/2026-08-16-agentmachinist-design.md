# AgentMachinist v0.1 Design

**Date:** 2026-08-16
**Status:** Approved (stack: Python + Click; spec-gen: local + CI from day one)

## What it is

AgentMachinist is a local-first, open-source agentic build/CI system for a solo
developer on a Mac. It bridges GitHub issues to local coding harnesses
(Claude Code, OpenCode, PI, Codex) through a three-phase pipeline:

1. **Spec** — an issue labeled `agent-task` triggers the configured harness to
   write an implementation spec, committed to a new branch and opened as a
   **draft PR** referencing the issue.
2. **Approve** — a human reviews the spec in the draft PR and approves it by
   applying the `machinist:approved` label (directly, or via a
   `/machinist-execute` comment that a small GitHub Action converts to the label).
3. **Execute** — the local daemon (`machinist watch`) or a manual
   `machinist run <issue>` checks out the spec branch in an isolated workspace,
   invokes the harness to implement the spec, runs the configured test command,
   pushes commits to the PR branch, and marks the PR ready for review.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Language/CLI | Python ≥3.12, Click, managed by `uv` | The tool is a subprocess orchestrator (harness CLIs, `git`, `gh`); I/O-bound, so iteration speed and Pydantic's config validation beat Go's binary distribution for a solo-dev tool. |
| Config schema | Pydantic v2, `extra="forbid"` | Self-documenting schema; typos in `machinist.yaml` fail loudly with readable errors. |
| GitHub integration | Thin wrapper over the `gh` CLI (subprocess + `--json`) | `gh` handles auth via keychain locally and via `GITHUB_TOKEN` in Actions runners — one wrapper works identically in both contexts; zero token management in our code. |
| Approval signal | Label `machinist:approved` on the draft PR | Pollable (`gh pr list --label`), auditable, reversible. Draft→Ready is reserved as the *agent's* signal that implementation is complete. |
| Spec-gen locale | Both: local watch daemon (primary) and a GitHub Actions workflow (needs `ANTHROPIC_API_KEY` secret) | Both paths run the same `machinist spec <n>` command; the workflow is just a CI invocation of the CLI. |
| Workspace isolation | `git worktree` (default) or full clone | Worktrees share the object store — cheap concurrent task isolation. |
| Templates location | `src/machinist/templates/` (package data, read via `importlib.resources`) | Repo-root `templates/` would not exist after `uv tool install`. |

## Architecture

```
src/machinist/
├── cli.py            Click entrypoint: init | spec | watch | run | status
├── config.py         Pydantic schema + loader for machinist.yaml
├── github.py         GitHubClient over `gh`: draft PRs, labels, issue reads
├── workspace.py      git worktree/clone management, .machinist/ layout
├── harness/
│   ├── base.py       Harness ABC: spec_argv()/implement_argv() + runners
│   ├── claude_code.py, opencode.py, pi.py, codex.py
│   └── __init__.py   registry: harness name → adapter class
├── phases/
│   ├── spec.py       Phase 1 orchestration
│   └── execute.py    Phase 3 orchestration
└── templates/
    ├── machinist.yaml
    └── github/machinist-spec.yml, machinist-approve.yml
```

Unit boundaries: `config` knows nothing of GitHub; `github` knows nothing of
harnesses; `harness` adapters only build argv and run subprocesses; `phases`
compose the three. Each unit is testable with an injected subprocess runner —
no network or real `gh`/harness binaries in unit tests.

## `machinist.yaml` schema

```yaml
version: 1
harness:
  name: claude-code            # claude-code | opencode | pi | codex
  command: null                # optional executable override
  timeout_minutes: 30          # Phase 3 implementation budget (1–240)
  spec_timeout_minutes: 10     # Phase 1 spec budget (1–60)
github:
  repo: null                   # "owner/repo"; null = gh derives from origin
  labels:
    trigger: agent-task
    approved: "machinist:approved"
  poll_interval_seconds: 60    # ≥10
workspace:
  root: ~/.machinist/workspaces
  strategy: worktree           # worktree | clone
  cleanup: on_success          # always | on_success | never
  branch_prefix: agent/
tests:
  command: null                # e.g. "pytest -q"; null skips the test gate
```

Unknown keys are rejected (typo protection). `ConfigError` wraps YAML and
validation failures with the file path and a readable message.

## GitHub wrapper contract

`GitHubClient(repo: str | None, runner=subprocess.run)`:

- `get_issue(n)` → `Issue(number, title, body, url, labels)` via `gh issue view --json`
- `create_draft_pr(branch, base, title, body)` → `DraftPR(number, url)` via `gh pr create --draft`
- `ensure_label(name, color, description)` — idempotent via `gh label create --force`
- Nonzero `gh` exit → `GitHubError` carrying stderr. `--repo` appended only when configured.

Polling reads (`issues_with_label`, `approved_prs`) land with `watch` in the
next milestone; the wrapper's plumbing (`_gh`, `_gh_json`) is built now.

## Error handling

- All subprocess failures raise typed exceptions (`ConfigError`, `GitHubError`,
  `HarnessError`) that the CLI catches and renders as one-line messages with
  nonzero exit — no tracebacks for expected failures.
- Harness invocations run under their configured timeout; timeout kills the
  subprocess and fails the phase.
- The `/machinist-execute` comment workflow only honors comments whose
  `author_association` is OWNER/MEMBER/COLLABORATOR.

## Testing strategy

pytest; TDD. Unit tests inject a fake subprocess runner into `GitHubClient`
and harness adapters, asserting on constructed argv and parsing canned JSON.
CLI tested with `click.testing.CliRunner` in isolated filesystems. No test
touches the network or real binaries.

## Milestones

- **M0 (this pass):** project scaffold, config schema + loader, GitHub wrapper
  (draft PR path), harness abstraction + adapters (argv construction),
  `machinist init` fully working, workflow + config templates, stub
  `spec|watch|run|status` commands, tests green.
- **M1:** `phases/spec.py` + `machinist spec` end-to-end; workspace module.
- **M2:** `phases/execute.py` + `machinist run`; test gate; push + ready-for-review.
- **M3:** `machinist watch` polling daemon tying it together.

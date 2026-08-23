# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

AgentMachinist is a local-first, issue-to-reviewed-PR build pipeline for solo
developers. It connects GitHub issues to a coding harness (Claude Code,
OpenCode, Pi, or Codex) with a human-approved specification between planning
and implementation:

```text
issue + trigger label → spec commit → draft PR → SHA-bound approval
                    → implementation → test gate → ready PR → human merge
```

Python 3.12+, Click CLI (`machinist`), pydantic config, packaged with
hatchling, published to PyPI as `agentmachinist` (current release: 0.7.0).
This repository dogfoods itself: the root `machinist.yaml` configures the
pipeline for this repo (`spec_source: github-actions`, test gate
`uv run pytest`).

## Commands

```sh
uv sync                                  # install (creates .venv)
uv run pytest                            # full suite (~5s, no network needed)
uv run pytest tests/test_harness.py      # one module
uv build                                 # sdist + wheel
uv run machinist --help                  # run the CLI from source
uv run machinist sync-workflows --check  # verify managed workflows match config
```

Tests never touch the network: `gh` and harness subprocesses are faked via
injected runners; git tests run against real repos in `tmp_path`.

## Architecture

The controller (this codebase) sits between four external systems: Git,
GitHub, a coding harness, and the repository's test command. **The controller
— never the harness — owns commits, pushes, PR transitions, and task
records.** AgentMachinist never merges; its boundary is a ready-for-review PR.

### Module map (`src/machinist/`)

- `cli.py` — Click entrypoints: `init [--harness --test-cmd]`, `doctor`,
  `sync-workflows [--check]`, `spec`, `approve`, `run [--force --retry]`,
  `watch [--once -v --interval]`, `retry [--phase --run]`, `status [-v]`,
  `update-check [--json --timeout]`,
  `clean [--issue --all --force]`, `inspect`. Ergonomics worth knowing:
  `init` auto-detects the test gate from the project manifest
  (`_detect_test_command`: pyproject/uv.lock → `uv run pytest`, package.json
  → `npm test`, Cargo.toml → `cargo test`, go.mod → `go test ./...`);
  `approve <n>` accepts a PR number *or* an issue number (falling back to the
  `<branch_prefix>issue-<n>` branch); `retry --run` and `run --retry` are the
  same recovery in either direction; `inspect <issue>` prints issue, PR,
  approval SHA, workspace path, and both Task Run records in one pass.
- `config.py` — strict pydantic schema for `machinist.yaml`
  (`extra="forbid"`: unknown keys fail loudly). Validates label shapes,
  branch prefix safety, timeout bounds. `harness.model` (str | None) and
  `harness.extra_args` (list[str]) are optional pass-throughs into adapter
  argv — the controller never interprets them.
- `github.py` — `GitHubClient`, a thin wrapper over the `gh` CLI (auth stays
  in `gh`; no tokens in this codebase). Also parses/writes the approval
  marker comment.
- `workspace.py` — isolated per-task checkouts (git worktree by default, or
  clone) under `workspace.root` (`~/.machinist/workspaces`); commit, leased
  push (`--force-with-lease`), cleanup policies. Directories are named
  `<repo-root-name>-issue-<n>`; `workspace_for_task`, `list_workspaces`, and
  `remove_workspace` encode that convention and back `machinist clean`.
  `remove_workspace` prefers `git worktree remove [--force]` + `prune` and
  only falls back to `rmtree` when that fails or the strategy is `clone`.
- `lifecycle.py` — durable Task Run records at
  `.machinist/runs/issue-<n>-<phase>.json` (atomic tmp+fsync+rename writes),
  `flock`-based local claims, explicit retry, checkpoints for crash recovery.
- `harness/` — `base.py` owns subprocess mechanics, timeouts, 30s heartbeat
  callbacks, and credential scrubbing (removes `GH_TOKEN`, `GITHUB_TOKEN`,
  askpass/SSH-agent vars; sets `GIT_TERMINAL_PROMPT=0`). Adapters
  (`claude_code`, `codex`, `pi`, `opencode`) only build argv for the two
  phases and stay one screen long. Registry in `__init__.py`. Every adapter
  threads `harness.model` (as `--model <value>`) and `harness.extra_args`
  into both `spec_argv` and `implement_argv`. **Order matters**: `claude-code`
  passes the prompt via `-p <prompt>`, so the new args append at the end;
  `codex`, `opencode`, and `pi` take the prompt as the final positional, so
  model/extra_args are inserted *before* the prompt is appended.
- `phases/spec.py` — Phase 1: issue → harness in read-only mode → spec file →
  branch → push → draft PR ("Closes #n"). Rejects empty specs and any
  working-tree change made by the harness.
- `phases/execute.py` — Phase 3: approval guards (label + SHA marker match +
  draft-ness), harness with edit permissions, git-custody postconditions,
  test-deletion guard (`limits.allow_test_deletions` opts out), test gate,
  commit, leased push, mark PR ready. The implement prompt lists the gate
  commands and asks the harness to iterate until they pass
  (`verification.harness_may_run_gates` opts out); the claude-code adapter
  allowlists exactly those commands via `Harness.allowed_commands`. Contains
  partial-push recovery via checkpoint evidence.
- `phases/status.py` — classifies pipeline state; `PIPELINE_STATES` is the
  canonical list (docs tests assert against it).
- `phases/watch.py` — one dispatch pass; the daemon loop lives in `cli.py`.
  Failed issues are never re-dispatched within a daemon lifetime.
- `workflows.py` — deterministic projection of config + installed version
  into managed `.github/workflows/` files (`machinist-spec.yml`,
  `machinist-approve.yml`); drift detection for `--check`/doctor.
- `doctor.py` — read-only diagnostics (git/gh/harness on PATH, gh auth, test
  gate configured, workflow drift, failed/abandoned Task Runs).
- `updates.py` — advisory release-update checks: reads the latest published
  version from PyPI's JSON API (bounded read, https-only, injectable opener),
  compares it with a PEP 440 subset parser (`parse_version`/`is_newer`; never
  claims an update from unparsable input, a yanked release, or a pre-release
  reached by a stable install), and derives the upgrade command from how the
  copy was installed (uv tool, pipx, editable checkout, pip fallback). Every
  failure degrades to `UpdateStatus.UNKNOWN`; `MACHINIST_NO_UPDATE_CHECK`
  disables the probe, and `tests/conftest.py` sets it so the suite stays
  offline.
- `notify.py` — best-effort desktop notifications: macOS `osascript` first,
  then Linux `notify-send`, each gated on `shutil.which`. All failures
  deliberately swallowed; stdout remains the record of what happened.
- `templates/` — `spec-prompt.md` and `implement-prompt.md`
  (`string.Template`), the `machinist.yaml` starter, and the GitHub workflow
  templates. Packaged into the wheel; release workflows smoke-test their
  presence.

## Domain language (see CONTEXT.md for the full glossary)

Use these terms exactly in docs and messages: **Task** (issue in the
pipeline), **Phase** (Spec or Execute — Approve is a human Gate, not a
Phase), **Spec** (identified by its exact commit), **Approval** (authorizes
one exact Spec commit; stale when the branch head changes), **Task Run**
(durable record of one Phase attempt), **Claim** (exclusive local ownership),
**Workshop** (the isolated checkout — code keeps the public name `Workspace`
for compatibility; docs say Workshop), **Harness**, **Evidence**.

## Core invariants — preserve these

1. **Git custody**: the harness must not commit, push, or touch
   `.machinist/`. Prompts say so (advisory); postconditions in
   `execute.py` enforce it (new HEAD, changed remote SHA, or `.machinist/`
   edits abort the run). Spec phase rejects any dirty tree.
2. **SHA-bound approval**: execution requires the approval label AND a
   trusted comment marker `<!-- agentmachinist:approval sha=<head-sha> -->`
   matching the current PR head. Marker authors must be
   OWNER/MEMBER/COLLABORATOR or github-actions. GitHub's review Approve
   button is *not* the mechanism.
3. **Draft-ness outranks the label**: a non-draft PR is "in review" and never
   re-executable without `run --force` (which demands fresh approval).
4. **Leased pushes**: implementation pushes use `--force-with-lease` against
   the approved SHA so concurrent remote changes fail loudly.
5. **Single dispatcher**: `github.spec_source` (`local` | `github-actions`)
   decides who owns Phase 1; managed workflows are projected from config,
   never hand-edited (the next sync intentionally replaces drift).
6. **Explicit retry only**: a failed Task Run blocks re-runs until
   `machinist retry`; a crash after push is reconciled from checkpoints
   (tests rerun, harness does not).
7. **Security wording**: never claim a harness "has no Git access" — the
   trust model (docs/trust-model.md, SECURITY.md) is credential *reduction*
   and detection, not OS-level isolation. `pull_request_target` automation
   must never check out or execute PR-head code.

## Conventions

- TDD is the house style: lifecycle/behavior changes start with a failing
  contract test. Keep `gh` construction behind `GitHubClient`, git behind
  `Workspace`, eligibility/persistence in lifecycle and phase modules.
- Conventional-commit prefixes: `feat:`, `fix:`, `docs:`, `chore:`, with
  optional scope (`feat(cli):`, `docs(spec):`).
- `tests/test_docs.py` enforces documentation drift: documented subcommands
  and flags must exist in the CLI, YAML blocks must validate against the
  config schema, pipeline states must match `PIPELINE_STATES`, and stale
  milestone/release claims are blocked. If you change the CLI, config, or
  states, update README/docs in the same change or the suite fails.
- Harness adapters have exact-argv tests (`tests/test_harness.py`). Changing
  an adapter means updating its argv test, `docs/harnesses.md`, and the
  changelog together. New pass-through options must be added to both
  `spec_argv` and `implement_argv` on all four adapters, respecting each
  one's prompt position.
- When config affects GitHub Actions: edit the template under
  `src/machinist/templates/github/`, update projection tests, then run
  `uv run machinist sync-workflows` and commit the reviewed projection.
- Update `CHANGELOG.md` and user docs for any command, config, state, trust,
  or compatibility change.
- Never commit secrets, generated Task Run files (`.machinist/runs/`), or
  retained workspaces.

## Repository layout notes

- `.machinist/specs/` — committed specs produced by the pipeline for this
  repo's own issues (dogfooding evidence, kept).
- `tasks/todo.md` — milestone log (M1 spec phase, M2 execute, M3 watch
  daemon, beta-readiness sweep, 0.2 reliability hardening — all complete).
  Append progress there when doing milestone-style work.
- `docs/superpowers/specs/` — the two design documents (initial design,
  reliability/usability hardening) that drove the current architecture.
- `docs/` — getting-started, architecture, operator-runbook, trust-model,
  harnesses matrix, plus three HTML visual assets: onboarding.html and
  first-run-guide.html (both structure- and link-checked by
  `tests/test_docs.py`) and explainer.html, an animated system walkthrough
  that no test or doc currently references.
- `AgentMachinist-Prompt.md` — the original kickoff prompt, historical.

## Releasing

PyPI Trusted Publishing (OIDC, no stored tokens). Bump `pyproject.toml`,
update `CHANGELOG.md`, run `uv lock` + tests + `uv build`, push, then publish
a GitHub Release tagged `v<version>`. The release workflow enforces
tag/version equality, reruns the suite, smoke-tests the installed wheel
(including packaged templates), and publishes last.

## Current state (2026-08-20)

- v0.7.0 is the current release; 0.6.0 retries the release smoke test
  while PyPI's index propagates, and 0.7.0 adds
  `machinist update-check` (with a matching advisory `doctor` row) that
  compares the installed release against PyPI. CI runs on
  Ubuntu and macOS across Python
  3.12, 3.13, and 3.14, plus minimum-dependency, Ruff, mypy, coverage,
  package, and aggregate `CI gate` jobs.
- Safe recovery, Spec revision/abandonment, amendments, cancellation, queue
  controls, named verification gates, notifications, run inspection,
  repository portfolios, and the managed macOS watcher service all ship.
- All designed commands ship; two full issue→merge lifecycles have run
  end-to-end (issue #1 and issue #4), the second fully daemon-driven.
- Known limits: macOS is the proven OS for the daemon; Linux notifications
  exist via `notify-send` but are unexercised in practice. claude-code is the
  proven harness (other adapters' flags verified against docs, less
  exercised). The CI spec workflow requires an `ANTHROPIC_API_KEY` repository
  secret and installs Claude Code regardless of the configured local harness.

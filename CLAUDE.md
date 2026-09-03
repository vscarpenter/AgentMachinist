# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

AgentMachinist is a local-first, issue-to-reviewed-PR build pipeline for solo
developers. It connects GitHub issues to a coding harness (Claude Code,
OpenCode, Pi, or Codex) with a human-approved specification between planning
and implementation:

```text
issue + trigger label → spec commit → draft PR → SHA-bound approval
                    → implementation → test gate → independent review
                    → ready PR → human merge
```

Python 3.12+, Click CLI (`machinist`), pydantic config, packaged with
hatchling, published to PyPI as `agentmachinist` (current release: 0.12.1).
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
  `review`, `amend`, `watch [--once -v --interval]`,
  `retry [--phase --run]`, `status [-v]`, `update-check [--json --timeout]`,
  `clean [--issue --all --force]`, `inspect`. Ergonomics worth knowing:
  `init` auto-detects the test gate from the project manifest
  (`_detect_test_command`: pyproject/uv.lock → `uv run pytest`, package.json
  → `npm test`, Cargo.toml → `cargo test`, go.mod → `go test ./...`);
  `approve <n>` accepts a PR number *or* an issue number (falling back to the
  `<branch_prefix>issue-<n>` branch); `retry --run` and `run --retry` are the
  same recovery in either direction; `inspect <issue>` prints issue, PR,
  approval SHA, workspace path, and all Task Run records in one pass. Click
  owns validation, rendering, notifications, and the daemon loop; it delegates
  claimed Phase construction to `dispatch.py`.
- `config.py` — strict pydantic schema for `machinist.yaml`
  (`extra="forbid"`: unknown keys fail loudly). Validates label shapes,
  branch prefix safety, and timeout bounds. The validated model also owns the
  sparse starter projection and compatibility-resolved effective projection.
  `config_cli.py` renders or persists those values without restating defaults.
  `harness.model` (str | None) and
  `harness.extra_args` (list[str]) are optional pass-throughs into adapter
  argv — the controller never interprets them.
- `dispatch.py` — the only constructor for claimed Spec, Execute, and Review
  Task Runs. It wires Claims, Harnesses, Workshops, cancellation, verification,
  and Phase functions; commands and watcher callbacks consume this interface.
- `evidence.py` — typed reads and Phase-aware validation for known Task Run
  Evidence. Persistence remains an open JSON mapping so historical and future
  unknown keys remain readable.
- `github.py` — `GitHubClient`, a thin wrapper over the `gh` CLI (auth stays
  in `gh`; no tokens in this codebase). Also parses/writes the approval
  marker comment.
- `repository_custody.py` — binds the GitHub client to the controller origin
  host/repository and verifies exact same-repository PR number, branch, base,
  head, state, and draft expectations.
- `workspace.py` — isolated per-task checkouts (git worktree by default, or
  clone) under `workspace.root` (`~/.machinist/workspaces`); commit, leased
  push (`--force-with-lease`), cleanup policies. Directories are named
  `<repo-root-name>-issue-<n>`; `workspace_for_task`, `list_workspaces`, and
  `remove_workspace` encode that convention and back `machinist clean`.
  `remove_workspace` prefers `git worktree remove [--force]` + `prune` and
  only falls back to `rmtree` when that fails or the strategy is `clone`.
- `lifecycle.py` — durable Task Run records at
  `.machinist/runs/issue-<n>-<phase>.json` (atomic tmp+fsync+rename writes),
  `flock`-based local claims, explicit retry, checkpoints for crash recovery,
  and the only parser for projection/journal discovery and corrupt/orphan
  artifact meaning.
- `transitions.py` — canonical pipeline state vocabulary, priority, dispatch
  eligibility, Task Run disposition, and next-action decisions. Status,
  watcher planning, explain, and observability consume its typed decisions.
- `verification.py` — the sole Verification Gate engine after Harness work,
  including required/advisory outcomes, mutation checks, cancellation,
  timeouts, logs, and Evidence projection.
- `harness/` — `base.py` owns subprocess mechanics, timeouts, 30s heartbeat
  callbacks, and credential scrubbing (removes `GH_TOKEN`, `GITHUB_TOKEN`,
  askpass/SSH-agent vars; sets `GIT_TERMINAL_PROMPT=0`). Adapters
  (`claude_code`, `codex`, `pi`, `opencode`) build a read-only argv profile
  (shared by Spec and Review) and an Execute edit profile, and stay one screen
  long. Registry in `__init__.py`. Every adapter
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
- `phases/review.py` — independent read-only review of the exact delivered
  Execute head; posts a bounded structured report and marks the PR ready only
  after rechecking custody.
- `phases/status.py` — projects GitHub and Task Run facts through
  `transitions.py`; its compatibility `PIPELINE_STATES` tuple derives from the
  canonical enum (docs tests assert against it).
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
pipeline), **Phase** (Spec, Execute, or Review — Approve is a human Gate, not a
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
   matching the current PR head. The controller trusts a marker only when
   the managed workflow authored it (`github-actions[bot]`); a marker typed
   by a human is not Approval, whatever their association. GitHub's review
   Approve button is *not* the mechanism. The managed approve workflow gates both
   paths on the actor before minting evidence: a `/machinist-execute` comment
   needs OWNER/MEMBER/COLLABORATOR, and the label path needs write or admin
   access, because GitHub grants label permission at triage level. The
   approver's login is recorded on the approval comment.
3. **Draft-ness outranks the label**: a non-draft PR is "in review" and never
   re-executable without `run --force` (which demands fresh approval).
4. **Leased pushes**: implementation pushes use `--force-with-lease` against
   the approved SHA so concurrent remote changes fail loudly.
5. **Single Task Run dispatcher**: every claimed Spec, Execute, and Review run
   is constructed by `TaskDispatcher`; Click and watcher code do not recreate
   Phase dependency wiring.
6. **Single Spec source**: `github.spec_source` (`local` | `github-actions`)
   decides who owns Phase 1; managed workflows are projected from config,
   never hand-edited (the next sync intentionally replaces drift).
7. **Explicit retry only**: a failed Task Run blocks re-runs until
   `machinist retry`; a crash after push is reconciled from checkpoints
   (tests rerun, harness does not).
8. **Security wording**: never claim a harness "has no Git access" — the
   trust model (docs/trust-model.md, SECURITY.md) is credential *reduction*
   and detection, not OS-level isolation. `pull_request_target` automation
   must never check out or execute PR-head code.

## Conventions

- TDD is the house style: lifecycle/behavior changes start with a failing
  contract test. Keep `gh` construction behind `GitHubClient`, git behind
  `Workspace`, Evidence interpretation in `evidence.py`, Task Run construction
  in `dispatch.py`, and transition decisions in `transitions.py`.
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

## Current state (2026-09-03)

- v0.12.1 is the current release. It fixes three independent Review defects
  found while dogfooding: Review named its ephemeral preview clone in a shape
  `Workspace` rejects (it now uses `preview-review-issue-<n>-<hex>`, matching
  Spec); the report parser accepts exactly one JSON object even when the
  harness wraps it in a Markdown fence or prefixes a sentence, and still fails
  closed on two objects or trailing prose; and the Review prompt now states
  the report contract (one bare JSON object, severity and confidence limited
  to low, medium, or high). v0.12.0 concentrated Task Run Evidence, journal
  inventory, Phase transitions, repository custody, Verification Gate
  execution, Phase dispatch, and configuration projection in deep internal
  modules while preserving the version-1 persistence and CLI contracts. The
  operating, architecture, trust, visual, historical, and TLDR documentation
  now describes that ownership model consistently. v0.11.0 made `doctor` the
  single first-run health check: a new read-only `task template` check closes
  the one managed file
  nothing verified, and every FAIL now prints an exact remediation keyed on a
  canonical name (`DOCTOR_CHECK_NAMES` in `doctor.py`) rather than matched
  against rendered text — a new check without a fix fails a test. `init` and
  `onboard` gained `--yes` (safe defaults *plus* the auto-detected test
  command); `--no-input` deliberately keeps its stricter contract and still
  will not convert a detected manifest into a test-gate guarantee. First run
  now writes a ~20-line `machinist.yaml` that is semantically identical to the
  packaged 94-line reference, and `--help` is grouped by workflow stage.
  v0.10.0 added independent Review, guided
  onboarding and rehearsal, Harness plugins with provider-aware CI,
  explain/live status, structured Task intake, and local aggregate reports with
  opt-in OTLP export. Its plugin-capable Harness identifier remains compatible
  with the declared Pydantic 2.7 minimum. v0.9.0 made first-run readiness
  explicit and added
  durable progress and attempt history, gives recovery paths exact commands,
  safeguards active Claims during service lifecycle operations, and hardens
  harness authentication and session isolation. `machinist doctor --run-gates`
  now provides the authoritative preflight, while `sync-labels --check/--apply`
  makes label setup inspectable. Managed-workflow drift remains visible in
  `watch`, `update-check`, and `doctor`; upgrading the package does not update
  checked-in workflows, so repositories must run `machinist sync-workflows`.
  Both managed approval paths require write or admin access and record the
  approver's login on the approval comment. Existing repositories must run
  `machinist sync-workflows` to adopt it. 0.8.1 fixed a false Git-custody failure under
  `workspace.strategy: worktree` (issue #16). A worktree shares `config`,
  `hooks/`, and `info/` with its parent, so the custody guard was
  byte-comparing the developer's own `.git/config`. Config files a Workshop
  *shares* now compare by sensitive key (`gitconfig.py`, a subprocess-free
  parser); everything a Workshop owns, plus hooks and `info/`, stays
  byte-strict. Custody checkpoint version is now 2, so a Workshop retained
  across the upgrade needs `machinist retry`. 0.8.0 added three AI-native SDLC
  playbook adoptions: the harness verification feedback loop
  (`verification.harness_may_run_gates` opts out), the test-deletion guard
  (`limits.allow_test_deletions` opts out), and the spec template's
  `## Risks` section with the standalone-readable quality bar. 0.7.0 added
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
  exercised). The CI spec workflow now installs the selected Spec adapter from
  its descriptor and requires that adapter's declared repository secret.

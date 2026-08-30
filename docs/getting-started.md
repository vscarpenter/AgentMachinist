# Getting Started with AgentMachinist

AgentMachinist turns a GitHub issue into a draft specification PR, waits for
approval of that exact spec commit, then asks your coding harness to implement
it. The resulting PR is still yours to review and merge.

## What is AgentMachinist?

It is an issue-to-reviewed-PR build pipeline with two human gates:

```text
issue → SPEC → draft PR → APPROVE exact SHA → EXECUTE → tests → ready PR
          machine                 human             machine       human
```

AgentMachinist owns Git operations and GitHub transitions. Each task runs in a
separate worktree or clone, so your active checkout is not used as the harness
workspace. The harness never receives merge authority from AgentMachinist.

## Before you begin

Install and verify:

| Tool | Check |
| --- | --- |
| Git | `git --version` |
| GitHub CLI, authenticated | `gh auth status` |
| uv | `uv --version` |
| One harness | for example, `claude --version` |

You need push and pull-request access to the target GitHub repository.
The core CLI is tested on macOS and Linux with Python 3.12–3.14. The managed
LaunchAgent integration is macOS-only; Linux users can schedule
`machinist watch --once` with their existing service manager.

## Install

```sh
uv tool install agentmachinist
machinist --version
```

Upgrade later with `uv tool upgrade agentmachinist`.

To find out whether an upgrade is waiting, run:

```sh
machinist update-check
```

It reads the latest published release from PyPI, compares it with the copy you
are running, and — when a newer release exists — prints the upgrade command
that matches how this copy was installed (`uv tool upgrade`, `pipx upgrade`,
`pip install --upgrade`, or `git pull && uv sync` for a source checkout). The
command runs without a repository or a `machinist.yaml`, exits non-zero only
when PyPI could not be reached, and supports `--json` for scripts.
`machinist doctor` runs the same check and reports an available release as a
warning.

Run inside a configured repository, it adds one more line: whether the managed
GitHub workflows still match this installation. Managed workflows are projected
files, so a workflow change takes effect only after `machinist sync-workflows`.
Upgrading the package alone can leave the previous workflow in place, and that
line is how you find out without running `doctor`. `machinist watch` prints the
same advisory at startup. It never blocks a command and never appears in
`update-check --json`.

Set `MACHINIST_NO_UPDATE_CHECK=1` to disable both probes; nothing else in the
pipeline contacts PyPI.

## Set up your repository

From the repository root:

```sh
machinist init
```

This creates `machinist.yaml`, `.machinist/specs/`, the managed approval
workflow, and the configured labels. It also idempotently adds
`/.machinist/runs/` to `.gitignore` so runtime records are not committed. It
does not overwrite an existing config unless you pass `--force`.

In a terminal, `init` walks you through the choices that matter on the first
run, each with a one-line explanation and a safe default:

- **Dispatch mode** — `local` (the `machinist watch` daemon runs the Spec
  phase on your machine) or `github-actions` (CI runs it; requires an
  `ANTHROPIC_API_KEY` repository secret).
- **Managed workflows** — install the Machinist-owned
  `.github/workflows/machinist-*.yml` files.
- **Harness** — `claude-code`, `codex`, `opencode`, or `pi`. With
  `github-actions` dispatch and managed workflows, the harness is
  `claude-code` because the managed CI spec workflow currently supports only
  a claude-code Spec harness.
- **Test gate** — confirm the auto-detected command, or pick your language
  for a suggested one (`pytest`, `npm test`, `cargo test`, `go test ./...`,
  `mvn test`), type your own, or skip the gate.
- **Notifications** — failures only, all key events, or none.

Flags pre-answer their questions and skip them: `--spec-source`, `--harness`,
`--test-cmd`, `--workflows/--no-workflows`, and `--notifications`. Passing
`--no-input`, or running without a terminal (CI, pipes), skips every question
and uses safe defaults. Manifest detection may print a test-command suggestion,
but non-interactive setup does not enable it unless `--test-cmd` is explicit.

`--no-workflows` is the explicit externally-managed mode. It writes
`github.manage_workflows: false`, skips workflow generation, and makes
`doctor` report that managed drift checking is disabled. To return ownership to
AgentMachinist, set that field to `true`, run `machinist sync-workflows`, review
the generated files, and commit them.

If you skipped the test-gate question or ran non-interactively, set a real test
gate before committing (or rerun `init --force --test-cmd "<command>"`):

```yaml
tests:
  command: uv run pytest
```

Then verify the installation without changing it:

```sh
machinist doctor --run-gates
machinist sync-labels --check
machinist sync-workflows --check
```

Resolve every `FAIL`. Treat a warning that no verification gates are configured
as an explicit decision, not a harmless default.

Review and persist the setup before the first task. The approval workflow must
exist on GitHub before a comment or label can record SHA-bound approval:

```sh
git status --short
git add machinist.yaml .machinist/specs/.gitkeep .gitignore
git add -p .github/workflows
git diff --cached
git commit -m "chore: configure AgentMachinist"
git push
```

If your repository already ignored `.machinist/runs/`, the initializer leaves
that rule unchanged. Omit an unchanged `.gitignore` from the staged files. Do
not commit anything until the staged diff matches the configuration you intend
to run.

## Your first agent task

Create a focused issue with acceptance criteria and apply the `agent-task`
label. With the default local dispatcher, start one pass:

```sh
machinist watch --once
```

Or address a specific issue directly:

```sh
machinist spec 7 --dry-run
machinist spec 7
```

`--dry-run` is a read-only preview: it prints the proposed Spec without a
commit, push, or PR. The normal command reads issue 7, provisions an isolated
workspace, runs the harness in its spec mode, rejects repository mutations,
writes `.machinist/specs/issue-7-spec.md`, commits and pushes `agent/issue-7`,
and opens a draft PR.

## Review and approve

Read the spec in the draft PR. Approval records both the configured label and
the exact 40-character PR head SHA. Choose one method:

```sh
machinist approve --pr 8
# or: machinist approve --issue 7
```

The positional form remains available when a number identifies only one Task.
Use `--pr` or `--issue` when an issue and pull request have the same number;
AgentMachinist refuses an ambiguous positional target.

Or post the exact PR comment:

```text
/machinist-execute <full-spec-commit-sha>
```

Copy the full SHA-bound command from the Spec PR body. The managed workflow
first requires write or admin access and refuses the approval if the PR head
changed before the job ran. Applying the configured
`machinist:approved` label manually binds the approval to the head SHA carried
by that label event, so a queued force-push cannot silently authorize new code.
Both paths require write or admin access, because association and label
permission can be weaker than push authority. Either way the approver's login
is recorded on the approval comment. `machinist approve` requests this workflow
transition; `machinist status` remains `awaiting approval` until the workflow
has verified and recorded it. `approval pending` means a label is already
visible without trusted SHA Evidence, as can happen briefly on the manual-label
path.

If anyone changes the spec branch afterward, `machinist status` reports
`approval stale` and execution refuses. Approve the new head again.

To regenerate a successful Spec from the current issue on its existing branch
and draft PR, run:

```sh
machinist spec 42 --revise
```

Review the regenerated diff and approve its new head. `machinist retry` remains
the recovery command for failed attempts; it is not the successful-Spec
revision path.

If the Spec should be rejected instead, abandon it explicitly:

```sh
machinist spec 42 --abandon --reason "requirements changed"
```

`--reason` is optional. Abandonment records the outcome, removes the issue's
trigger label and the PR's approval label, and closes the open draft PR. It
does not merge or delete the branch.

Do not mark the draft ready yourself. AgentMachinist uses that transition to
signal that implementation and the configured test gate completed.

`machinist run <issue> --force` is an intentional rework path for a ready PR.
It does not bypass immutable approval: approve that PR's current head again
before forcing a second implementation attempt.

For feedback-driven rework, prefer the explicit amendment command. The ready
PR's current head still needs fresh approval, and exactly one feedback source
is required:

```sh
machinist approve --issue 42
machinist amend 42 --feedback "Keep the public API; add the missing edge-case test."
# or: machinist amend 42 --feedback-file review-notes.txt
```

Amendment always provisions a fresh Execute attempt from the approved remote
head. It never imports manual edits from a retained failed workspace.

## Spec generation: local or CI

`github.spec_source` assigns Phase 1 to exactly one dispatcher:

```yaml
github:
  spec_source: local
```

- `local` means `machinist watch` generates specs using your local harness
  login. The managed spec workflow is absent.
- `github-actions` means `watch` leaves labeled issues alone and
  `machinist-spec.yml` owns spec generation. Run `machinist sync-workflows`
  after changing the value.

`github.spec_install` chooses how CI obtains the controller: `pypi` (default)
pins `agentmachinist==<installed version>`; `checkout` runs `uv sync --frozen`
and `uv run machinist spec` from the repository (for dogfooding AgentMachinist
itself). The generated workflow is still Claude-Code-specific and requires
`ANTHROPIC_API_KEY`. For other harnesses, use local mode unless you maintain
an appropriate installation step.

## Configuration reference

```yaml
version: 1

harness:
  name: claude-code
  command: null
  model: null
  extra_args: []
  timeout_minutes: 30
  spec_timeout_minutes: 10
  spec: null
  execute: null

instructions:
  spec:
    paths: []
    append: null
  execute:
    paths: []
    append: null

github:
  repo: null
  spec_source: local
  spec_install: pypi
  manage_workflows: true
  labels:
    trigger: agent-task
    approved: "machinist:approved"
  poll_interval_seconds: 60

workspace:
  root: ~/.machinist/workspaces
  strategy: worktree
  cleanup: on_success
  branch_prefix: agent/

tests:
  command: null

verification:
  gates: []
  harness_may_run_gates: true

queue:
  max_tasks_per_pass: 1
  allowed_hours: null
  task_budget: null

notifications:
  backend: desktop
  events: [failure]
  command: null
  webhook: null

limits:
  max_issue_body_chars: 50000
  max_spec_chars: 100000
  max_changed_files: 100
  max_changed_bytes: 5242880
  denied_paths: [.machinist]
  allow_binary: false
  allow_test_deletions: false
```

Unknown keys fail validation. With `repo: null`, AgentMachinist derives and
binds the exact GitHub host, owner, and repository from the controller's Git
origin; an explicit `repo` must match it. `workspace.strategy` is `worktree` by
default, which is fast because the Workshop shares your repository's object
store. It also shares `config`, `hooks/`, and `info/` with your checkout, so
the Git metadata custody guard watches files you edit yourself, and installing
a hook while a Task runs will stop that Task. Choose `clone` when you want each
Workshop to own its Git metadata; see
[the trust model](trust-model.md) for what the guard covers. Workspace cleanup
can be `always`, `on_success`, or `never`;
keeping failed workspaces is useful for diagnosis. `tests.command: null` skips
the legacy command; verification is skipped only when no named
`verification.gates` are configured, which is surfaced as a doctor warning.
The later sections describe the phase profiles, named gates, admission
controls, notifications, and safety limits.

Use the config commands before starting a Task:

```sh
machinist config validate
machinist config show
machinist config show --json
machinist config schema --output machinist.schema.json
machinist config set queue.max_tasks_per_pass 2
```

`config show` resolves the effective Spec and Execute harness profiles.
`config set` parses its value as YAML, validates the complete document, writes
atomically, and rewrites the file as canonical YAML; comments are normalized.
`validate`, `show`, and `set` accept `--path` for a non-default config file.

After changing labels, dispatcher ownership, or the installed package version,
regenerate workflows:

```sh
machinist sync-workflows
git diff -- .github/workflows
```

## Phase harnesses and instruction overlays

The top-level harness remains the default for both phases. Override only the
fields that differ for Spec or Execute:

```yaml
harness:
  name: claude-code
  model: null
  extra_args: []
  timeout_minutes: 30
  spec_timeout_minutes: 10
  spec:
    model: claude-sonnet-4-5
    timeout_minutes: 12
  execute:
    name: codex
    model: gpt-5.3-codex
    timeout_minutes: 45
```

Omitted fields inherit from the top level. When a phase changes harness
provider, its command, model, and extra arguments do not accidentally inherit
from the other provider. Adapter-owned sandbox, permission, model, session,
and tool flags are rejected from `extra_args`.

Repository-specific guidance can be appended independently to each prompt:

```yaml
instructions:
  spec:
    paths: [docs/product-rules.md]
    append: "Preserve the public CLI contract."
  execute:
    paths: [AGENTS.md, docs/testing.md]
    append: null
```

Paths are ordered, UTF-8, repository-relative files. AgentMachinist rejects
path escapes, duplicate resolutions, NUL bytes, excessive files, and an
oversized combined overlay before invoking a harness.

## Verification gates and change limits

For one legacy gate, keep `tests.command`. For ordered, separately reported
checks, set `tests.command: null` and configure named gates instead:

```yaml
tests:
  command: null
verification:
  gates:
    - name: unit tests
      command: uv run pytest -q
      timeout_minutes: 30
      required: true
      mutation_policy: forbid
    - name: advisory dependency report
      command: ./scripts/dependency-report.sh
      timeout_minutes: 5
      required: false
      mutation_policy: forbid
limits:
  max_issue_body_chars: 50000
  max_spec_chars: 100000
  max_changed_files: 100
  max_changed_bytes: 5242880
  denied_paths: [.machinist, secrets]
  allow_binary: false
```

Required command failures block PR readiness. Ordinary advisory command
failures are preserved as evidence without blocking readiness, and advisory
gates must be read-only. Cancellation, forbidden working-tree mutation, or an
inability to take the before/after snapshot always blocks fail-closed,
regardless of `required`. Configure either named gates or `tests.command`,
never both. Execute also enforces changed-file, changed-byte, denied-path, and
binary-file limits before the controller commits or pushes.

By default (`verification.harness_may_run_gates: true`) the implementation
prompt also lists the gate commands and asks the harness to run each required
gate itself and iterate until it passes before finishing — fixing the code,
never weakening a test. For `claude-code`, whose headless edit mode otherwise
denies command execution, exactly those commands are allowlisted. The
controller still runs every gate afterwards; the harness's own runs only
improve first-pass quality. Set `harness_may_run_gates: false` to keep the
gate commands out of the harness entirely. Gate runs by the harness count
against `harness.timeout_minutes`, so budget the timeout for at least one
full gate cycle.

As the deterministic backstop for "fix the code, never the tests", Execute
refuses to commit an implementation that deleted a test file (matched by
common path heuristics: `tests/`, `test/`, `__tests__/`, and `spec/`
directories, plus `test_*`, `*_test.*`, `*.test.*`, `*.spec.*`, and
`conftest.py` basenames). Renaming a test file appears as a deletion and is
also refused. When an approved Spec legitimately removes or renames tests,
set `limits.allow_test_deletions: true` for that run and turn it back off
afterwards. Modified tests are not flagged — updating tests is normal Spec
work — so weakened assertions still need human review on the PR.

## Choosing a harness

Set `harness.name` to `claude-code`, `opencode`, `pi`, or `codex`. Local runs
reuse the provider authentication already available to that executable.

```yaml
harness:
  name: codex
  command: /opt/homebrew/bin/codex
```

Support is not identical. Some spec modes have a CLI-enforced read-only tool or
sandbox boundary; OpenCode's plan agent is advisory. All implementations are
checked afterward for harness-created commits, remote branch changes, and
`.machinist/` changes. See the [harness matrix](harnesses.md) and
[trust model](trust-model.md) before unattended operation.

`harness.model` selects an adapter-specific model. `harness.extra_args` appends
arguments to both Spec and Execute invocations after AgentMachinist's own
arguments. AgentMachinist rejects adapter-owned sandbox, permission, model,
session, and tool flags, including duplicate forms that could override its
controls. Other additional arguments remain advanced and may change behavior
as harness CLIs evolve, so keep the list empty unless you have reviewed the
final command and its trust impact.

## Watch admission and operator controls

Preview eligibility and every deferral reason without dispatching work:

```sh
machinist watch --dry-run
machinist watch --once --max-tasks 1
```

`queue.max_tasks_per_pass` is the persistent per-poll limit; `--max-tasks`
overrides it for one invocation (`0` admits none). You can also limit dispatch
to a time window and daily budget:

```yaml
queue:
  max_tasks_per_pass: 1
  allowed_hours:
    start: "08:00"
    end: "20:00"
    timezone: America/Chicago
    days: [mon, tue, wed, thu, fri]
  task_budget:
    max_tasks_per_day: 5
    max_runtime_minutes_per_day: 240
    timezone: America/Chicago
```

Allowed-hour windows may cross midnight. Daily counts come from local Task Run
history, so these are conservative local admission controls, not distributed
quotas across several Macs.

Pause all new dispatches or defer one issue durably:

```sh
machinist queue pause --reason "traveling"
machinist queue show --json
machinist queue defer 42 --reason "waiting for API decision"
machinist queue allow 42
machinist queue resume
```

Pause and defer do not terminate a Task that is already running. To request a
cooperative stop and block future dispatch, use cancellation:

```sh
machinist cancel 42 --reason "requirements changed"
machinist cancel 42 --clear
```

Supervised harness and verification processes receive the cancellation and are
terminated as a process group. The durable marker keeps the watcher from
starting the issue again. Clear it directly, or use an explicit
`machinist retry` after the cancelled run; retry marks the run retryable and
clears its cancellation marker.

## Notifications and safety limits

Notifications are best-effort and never turn a successful Task into a failure.
Choose events from `failure`, `spec_ready`, `approval_stale`, and `pr_ready`:

```yaml
notifications:
  backend: desktop
  events: [failure, spec_ready, approval_stale, pr_ready]
  command: null
  webhook: null
```

Successful deliveries are deduplicated for 24 hours in the repository-local
`.machinist/runs/notification-ledger.json`. This survives short-lived
`watch --once` processes, including the managed LaunchAgent, while allowing a
reminder after the window. Failed, disabled, or filtered deliveries are not
recorded. If the bounded ledger is corrupt or unavailable, delivery fails open
with a warning so an important alert is not silently lost.

The other backends are `disabled`, `command`, and `webhook`. Command delivery
uses an argv list without a shell and sends event JSON on standard input.
Webhook configuration names environment variables for the URL and optional
authorization value; it does not put secrets in `machinist.yaml`.

```yaml
notifications:
  backend: command
  events: [failure, pr_ready]
  command:
    argv: [/absolute/path/to/notifier, --stdin-json]
    timeout_seconds: 5
  webhook: null
```

For a webhook, set `backend: webhook`, set `command: null`, and configure
`webhook.url_env`, optional `webhook.authorization_env`, and
`webhook.timeout_seconds`. Export the named values in the watcher's process
environment rather than committing them. A configured authorization value is
sent only to an HTTPS URL; authenticated plaintext HTTP is rejected. Webhook
redirects are rejected before another request can receive the payload or
authorization. The managed LaunchAgent intentionally uses a minimal environment
and does not copy arbitrary secrets from your shell; use desktop delivery there
unless you deliberately provision the named launchd environment values outside
AgentMachinist.

## macOS watcher service

On macOS, install, register, and immediately start a per-repository LaunchAgent
after the config and executable are ready:

```sh
machinist service install
machinist service status
machinist service logs --lines 100
```

The service runs `machinist watch --once` at
`github.poll_interval_seconds`, uses the repository as its working directory,
and writes logs under `.machinist/runs/service/`; `service logs --lines` prints a
bounded recent tail rather than following indefinitely. Lifecycle commands are
explicit:

```sh
machinist service start
machinist service restart
machinist service stop
machinist service uninstall
```

`stop` preserves the installed plist and logs. `uninstall` removes the plist
but deliberately preserves logs. `status` reports launchd registration, the
last completed watcher poll, health, and active Task Runs. Install, restart,
stop, and uninstall refuse to interrupt an active Claim; wait for it to finish
or pass `--force` only when termination is intentional. Service management
currently supports macOS launchd only.

## Local evidence and repository portfolio

Use the local read model when GitHub is unavailable or when scripts need JSON:

```sh
machinist status --local --json
machinist runs --issue 42 --json
machinist inspect 42 --offline --json
```

These reports include current and historical attempts plus orphaned, partial,
or corrupt runtime artifacts; remote-source errors do not erase readable local
evidence.

For several repositories on one Mac, maintain the optional registry and view
their local status together:

```sh
machinist repo add /absolute/path/to/project
machinist repo list --json
machinist status --all --json
machinist repo remove /absolute/path/to/project
```

Portfolio status is local-only. It reports an unavailable repository alongside
healthy ones instead of failing the entire view.

## Troubleshooting

Start with:

```sh
machinist doctor --run-gates
machinist status
machinist inspect 7
```

Common states and responses:

| State or error | Response |
| --- | --- |
| `awaiting spec` | Run local `watch`/`spec`, or verify the CI dispatcher. |
| `awaiting approval` | Review the draft spec. |
| `approval pending` | The label exists but SHA evidence has not been recorded; inspect the approval workflow, then approve again only if it failed. |
| `approval stale` | The branch changed after approval; approve the current head. |
| `approved` | Run `machinist run <issue>` or leave `watch` running. |
| `in review` | Implementation finished; review the PR. |
| `spec running` / `execute running` | Machinist holds the Claim; `status`/`runs` show its current named stage and elapsed time. |
| `spec interrupted` / `execute interrupted` | No process holds the recorded Claim; run the exact `Next:` retry command. |
| `spec failed` / `execute failed` | Inspect the retained Evidence, fix the cause, then use the displayed retry command. |
| `spec cancelled` / `execute cancelled` | Clear or replace the cancellation request before retrying. |
| `spec abandoned` / `execute abandoned` | The operator ended this lifecycle; retry only after deciding it should resume. |
| `spec closed` | The Spec PR is closed; revise the Task intent before starting another Spec. |
| Failed Execute run retained useful edits | Inspect the path, then run `machinist retry <issue> --phase execute --run --resume` from the repository root. |
| Failed Execute run should start clean | Run `machinist retry <issue> --phase execute --run --fresh`. Omitting both recovery flags also selects a fresh attempt. |
| Task should not start again | Run `machinist cancel <issue> --reason "..."`; clear it directly or explicitly retry only when dispatch is safe. |
| Queue or issue is intentionally waiting | Run `machinist queue show`; use `queue resume` or `queue allow <issue>` as appropriate. |
| Workspace already exists | Inspect it first, or prune it with `machinist clean --issue <issue>` or `machinist clean --all`. |
| Managed workflow drift | `watch` and `update-check` report it. Run `machinist sync-workflows`, inspect, commit, and push. |
| Configuration is unclear | Run `machinist config validate` and `machinist config show`; neither starts a Task. |
| GitHub is unavailable | Preserve local evidence with `machinist status --local`, `machinist runs`, or `machinist inspect <issue> --offline`. |
| launchd watcher is quiet | Run `machinist service status` and `machinist service logs --lines 100`. |
| Unsure whether the CLI is current | Run `machinist update-check`; it prints the upgrade command for this installation and flags managed-workflow drift. |
| `doctor` warns that PyPI is unreachable | The update probe is advisory. Set `MACHINIST_NO_UPDATE_CHECK=1` on offline machines. |

Task Run failures and recovery evidence live under `.machinist/runs/`. The
initializer adds `/.machinist/runs/` to `.gitignore`, and `doctor` fails its
runtime-state check if that protection is later removed. A failed implementation
is not silently retried. Checkpoints preserve evidence about partial push
progress, and `--resume` validates and reuses the retained managed workspace.
`--fresh` starts another workspace from the approved head. Fresh is the default
when neither recovery flag is supplied. Run retry from the repository root, not
from inside the retained workspace.

## Operational limits

- Local file locks prevent duplicate work on one machine/process family; they
  are not a distributed lock across multiple hosts. Run one local watcher per
  repository.
- Queue windows and daily budgets are local admission controls based on local
  history; they do not coordinate several watcher hosts.
- Harness processes run as your OS user. AgentMachinist reduces controller
  credentials and checks postconditions, but it is not a container or VM.
- Provider authentication, quotas, model behavior, and spec quality remain
  external dependencies.
- AgentMachinist produces a ready PR. It does not merge, deploy, or verify a
  deployed runtime.

For restarts, retries, logs, and cleanup, continue with the
[operator runbook](operator-runbook.md).

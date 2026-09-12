# Operator runbook

This runbook describes the current implementation, including the foreground
local workflow, optional GitLab intake/publication, and the readiness,
remote-base, and diagnostic behavior introduced in 0.15.0.
Install with `uv tool install agentmachinist`, or upgrade with
`uv tool upgrade agentmachinist`. See the
[installation instructions](getting-started.md#install) for other setups.

## Local foreground operation

Use the [local workflow guide](local-workflow.md) for the complete no-forge
journey. `machinist start` saves a local Task and stops at exact-SHA Approval;
`machinist approve --task T1 --spec-sha <sha>` continues Execute, verification, and
independent Review. `machinist status T1` reports the next valid action without
fetching forge state. Inspect the local report/diff before `machinist integrate T1`.

Start requires a clean named branch, initial commit, configured Git author,
installed/authenticated Harnesses, and at least one required Verification Gate.
Setup checks executables and Phase support; it does not probe authentication.
Use optional `doctor --local` for the available authentication checks.
Baseline verification runs in an isolated committed checkout before the Spec
Harness; ignored dependencies must be prepared by the Gate command or provided
externally. Local Review always runs.

Failed Phases require `machinist retry --task T1 --phase execute` (or
`--phase spec`/`--phase review`). Local retry runs immediately, clears
cancellation, and resumes Execute edits by default; `--fresh` chooses a fresh
Workshop. Local Execute resume requires the retained bytes to match the failure
checkpoint. If you change the configured Gates, limits, or instructions, use
`machinist retry --task T1 --phase execute --fresh`; those inputs invalidate
the matching recovery checkpoint. Inspect retained local edits without changing
them when you intend to resume. `machinist continue T1` does not replace explicit
retry or exact-SHA Approval. `machinist amend --task T1 --feedback <text>`
requires a completed reviewed candidate, generates a new Spec, and invalidates
Approval. Recover a failed Phase with retry first. Local amendment cannot revise
an initial Spec awaiting Approval; start a new Task with corrected intent if
you reject that Spec. After integration has begun, start a new Task from the
current base instead of amending that Task.

Publication is independent: `machinist publish T1 --provider gitlab` (or
`github`) can retry an uncertain push or change creation without repeating the
local Phases. Keep one persistent runner checkout per repository for a small
team; local Claims and Task records are not multi-host coordination.
GitLab supports nested namespaces and explicit self-managed hosts through
`--host`; the host must match origin. Authenticate `glab` for that host; GitLab
CI Spec dispatch and remote Approval are not implemented. Local orchestration
can use a cloud model; it does not establish offline inference.

### Optional local readiness

**New in 0.15.0:** these diagnostics are optional and need no forge setup or
saved local Task state.

```sh
machinist doctor --local
machinist doctor --local --json
```

No setup step is added. The check resolves the same local settings as `start`,
including first-run discovery without saving it, and inspects Git branch,
commit, identity, cleanliness, Workshop location, Harness Phase support and
available version/help/authentication probes, and required Gate entry points.
It creates no Task, Claim, Workshop, runtime/config file, exclusion, or ref and
does not invoke a model, forge, update probe, or Gate by default. JSON uses the
existing doctor report shape; failed checks return a nonzero exit status.
An available command or successful authentication probe does not prove passing
tests, model access, or quota.

The Git identity check follows the controller's existing fallback; it does not
require an extra author setup step. Runtime exclusion is checked without writing
it. Unsafe exclusion paths fail; an exclusion that still needs setup is a warning
because `start` must apply and verify it against the repository's ignore rules.

`machinist doctor --local --run-gates` explicitly executes configured Gates
after readiness failures are resolved. It uses the shared Verification engine
in the controller checkout; commands can write files or download dependencies.
This does not replace `start`'s isolated Workshop baseline check. Plain
`doctor` continues to inspect GitHub setup.

### Local settings and Evidence

First start saves applicable root settings once in
`.machinist/runs/local/config.yaml`. Subsequent local commands use this copy:

```sh
machinist config show --path .machinist/runs/local/config.yaml
machinist config validate --path .machinist/runs/local/config.yaml
machinist config set tests.command "uv run pytest" --path .machinist/runs/local/config.yaml
machinist status T1 --json
machinist status T1 --watch --interval 2
```

The `tests.command` example applies to the single-Gate form; edit
`verification.gates` when using named Gates. Shared schema validation does not
replace the local constraints checked on load: required verification, Review
enabled, local Spec source, managed workflows off, telemetry endpoint unset,
and an absolute Workshop root outside the repository. Repair a baseline Gate
failure here and run `machinist retry --task T1 --phase spec`. If the committed
baseline itself needs a fix, commit it and start a new Task from the new base.

Task records and reports are in `.machinist/runs/local/tasks/`; Phase projections
and history are under `.machinist/runs/local/`. Local setup uses Git's local
exclude file to keep runtime state untracked. Do not edit Task JSON or delete
retained recovery Evidence. Review the report path printed by status; the
candidate ref is `agent/task-1` for `T1` with the default branch prefix.

If integration fails, resolve a dirty checkout or select the recorded base
branch, then rerun `machinist integrate T1`. A changed base/candidate is a
conflict requiring a new decision, not authorization to force a merge. Repeat
an interrupted integration using its saved intent. For publication failures,
repeat the same `publish` command; resolve pending publication before amending.

### Command scope in mixed checkouts

With local configuration present, `status` lists local Tasks; `status T1`
selects one, and `--watch` follows local changes. It does not query GitHub.
Legacy `runs`, `inspect`, `explain`, `report`, and portfolio `status --all`
continue reading `.machinist/runs/` issue records; they do not aggregate local
Tasks. `status --local` reads legacy records only when local configuration is
absent. Use `runs` and `inspect` for legacy Evidence in a mixed checkout.

Plain `doctor` remains a GitHub setup preflight; `doctor --local`
checks local readiness. `watch`, `queue`, service
scheduling, admission budgets, and notifications belong to the legacy workflow;
they do not govern foreground Tasks. `clean` manages legacy Workshops and has
no `--task` selector. Local success cleanup follows `workspace.cleanup`; keep
failed local Workshops until you have selected a recovery action.

The remaining sections describe the legacy GitHub workflow unless explicitly
stated otherwise.

## GitHub preflight

Managed workflows pin the installed controller version. Consumer repositories
using `github.spec_install: pypi` need that exact version available on PyPI.
This repository's development workflows use `github.spec_install: checkout`.

Run from the configured repository root:

```sh
machinist doctor --run-gates   # single health check — prints the exact fix for any FAIL

# Read-only confirmations of one subsystem at a time. doctor already covers all
# three; reach for these only when you want to re-check one in isolation.
machinist sync-labels --check
machinist sync-workflows --check
machinist task template --check
machinist rehearse
machinist status
```

Resolve `FAIL` checks before unattended work. `doctor` also reports whether a
newer release is published; `machinist update-check` prints the same result
with the upgrade command for this installation, and
`machinist update-check --json` is the scriptable form. Both probes are
advisory: they never fail a pipeline command, and `MACHINIST_NO_UPDATE_CHECK=1`
disables them on offline hosts.

`update-check` also reports managed-workflow drift, and `machinist watch`
reports it at startup. A workflow fix ships in a projected file rather than in
library code, so upgrading the package alone leaves the previous workflow in
place; the advisory names `machinist sync-workflows` so the gap is not silent.
It never blocks a command and never appears in `update-check --json`. `doctor`
remains the check that fails on drift. Decide explicitly whether a
warning that no verification gates are configured is acceptable. A Task Run
warning points to a failed or process-abandoned record that should be inspected
and explicitly retried.

## GitHub run modes

- Interactive: `machinist watch`
- One scheduler-friendly pass: `machinist watch --once`
- Read-only admission preview: `machinist watch --dry-run`
- Manual phases: `machinist spec <issue>`, `machinist run <issue>`, and
  `machinist review <issue>` when `review.enabled: true` in root
  `machinist.yaml`. With Review disabled, Execute marks the PR ready itself;
  invoking `review` fails.

**Unreleased source checkout:** expanded completion output prints the next
action and a sample command. After
`machinist task new`, the suggestion follows `github.spec_source` and whether
`--dispatch` was used: generate the Spec manually, process eligible Tasks with
the watcher, add the trigger label for GitHub Actions, or wait for hosted Spec
generation. A completed Spec points to Approval; Execute points to independent
Review when enabled; completed delivery points to human PR inspection.
`watch --once` prints these Phase receipts too. Follow the receipt for the
completed operation; it does not run the suggested command automatically.

GitHub `machinist approve --issue <issue>` requests Approval asynchronously.
Before a manual `run`, wait for the managed approval workflow to succeed and
verify the PR has both the configured approval label and a
`github-actions[bot]` approval marker matching its exact current head.
`machinist inspect <issue> --json` exposes the full `head_sha` and trusted
`approval_sha`; they must match. Without local configuration, `status --watch`
can also show a draft PR become `approved`. A watcher waits for that state;
a premature manual `run` can create a failed Execute that needs explicit retry.

Run one local watcher per repository. The claim is local, not cross-host. If
`github.spec_source` is `github-actions`, the local watcher handles approved
execution but deliberately skips spec generation.

`watch` prints a managed-workflow drift advisory at startup when the projection
no longer matches this installation. It is a warning, not a gate: a package
upgrade must not stop a running daemon. Run `machinist sync-workflows` to clear
it.

On macOS, AgentMachinist can manage that scheduler integration directly;
`install` writes the plist, registers it, and starts it immediately:

```sh
machinist service install
machinist service status
machinist service logs --lines 100
```

The LaunchAgent runs `watch --once` from the repository at
`github.poll_interval_seconds`. `start` starts an installed service,
`restart` replaces its current process, and `stop` preserves its plist and
logs. `status` reports `loaded/scheduled` plus watcher health, the last
completed poll, and active Task Runs; the one-shot watcher is normally idle
between intervals. Install, restart, stop, and uninstall refuse while a Task
holds a Claim. Wait for it to finish, or use `--force` only when intentionally
terminating it. `start`, `restart`, `stop`, `status`, `logs`, and `uninstall`
remain available if `machinist.yaml` is missing or invalid or the installed
controller is no longer on the current `PATH`. Only `install` needs a valid
current config and executable. `uninstall` removes the managed plist but
preserves logs. For
another scheduler, set the working directory to the repository root, use an
absolute controller path, run `watch --once`, and capture stdout/stderr.

`service logs` reads and emits at most 64 KiB from each log as well as applying
the requested line limit. It prints an explicit truncation marker when either
limit hides older content, so a runaway or single-line log cannot flood the
operator terminal.

Identical successful notifications are remembered for 24 hours in
`.machinist/runs/notification-ledger.json`, so a new one-shot process does not
repeat the same failure or stale-approval alert every interval. Failed or
filtered deliveries remain eligible for another attempt; a corrupt or
unavailable ledger warns and fails open rather than suppressing an alert.

## GitHub watcher admission control

The configured `queue.max_tasks_per_pass` limits each poll; the
`watch --max-tasks <n>` option overrides it for one process. `watch --dry-run`
reports eligible and deferred Tasks without claiming or dispatching them.
Optional allowed hours and daily Task Run/runtime budgets are evaluated from
local time and legacy Task Run history. `queue.task_budget.max_runs_per_day`
counts Phase attempts, not unique issues: Spec, Execute, and Review each count.
The older `max_tasks_per_day` key loads as an alias with the same semantics;
conflicting values are rejected. These controls do not cap foreground local
work.

Use durable operator controls for planned pauses:

```sh
machinist queue pause --reason "maintenance"
machinist queue defer 42 --reason "waiting for product decision"
machinist queue show --json
machinist queue allow 42
machinist queue resume
```

Pause applies to all new dispatches; defer applies to one issue. Neither stops
a Task that already holds a claim. Corrupt queue state fails closed and is
reported by `queue show`/the watcher.

## Observe legacy GitHub work

In a checkout without local configuration, `machinist status` shows `awaiting spec`, `awaiting approval`,
`approval pending`, `approval stale`, `approved`, `awaiting review`, and
`in review`. Locally persisted issue-run
outcomes add `spec running`, `spec interrupted`, `spec failed`,
`spec cancelled`, `spec abandoned`, `spec closed`, `execute running`,
`execute interrupted`, `execute failed`, `execute cancelled`,
`execute abandoned`, `review running`, `review interrupted`, `review failed`,
`review cancelled`, and `review abandoned`. A persisted `running` projection without a held Claim is
reported as interrupted. `status`, `runs`, and `inspect` show named stages,
attempt history, elapsed time, and an exact `Next:` command where recovery is
available. A `retryable` persistence state projects back to remote eligibility
so the watcher can dispatch it; it is not shown as a second competing pipeline
state.

Legacy issue Task Run records are under `.machinist/runs/`. They are runtime state and
should remain ignored by Git. `machinist init` idempotently adds
`/.machinist/runs/` to `.gitignore`, and `doctor` reports a failure if the rule
is removed. Failed workspaces are retained by the default `cleanup: on_success`
policy; the error prints their path.

For scriptable legacy Evidence (and default/live status when no local
configuration is present), use:

```sh
machinist explain 42 --json
machinist status --watch --interval 2
machinist status --local --json
machinist runs --issue 42 --json
machinist inspect 42 --offline --json
machinist report --since 30d --json
```

The legacy local read model includes current/history records plus orphaned, partial,
and corrupt artifacts. Without `--offline`, inspection adds GitHub sources but
still preserves readable local evidence when a remote source fails.

`explain` is the side-effect-free policy view: it resolves effective phase
profiles, gates, limits, queue state, attempts, cancellation, workspace paths,
and the exact next action while showing credential names only. Live status
prints the initial pipeline snapshot and later changes; JSON watch mode is
newline-delimited and Ctrl-C exits successfully.

`report` aggregates local outcomes, retries, cancellations, durations,
gate-failure statuses, and safe Harness/model metadata. Export is opt-in through
`telemetry.otlp_endpoint` or `--otlp-endpoint`; authorization comes from
`MACHINIST_OTLP_AUTHORIZATION`. An export failure returns non-zero after the
local report and never modifies history or prints the response body.

For a solo portfolio, register canonical repository roots and read them without
changing directories:

```sh
machinist repo add /absolute/path/to/repository
machinist repo list
machinist status --all --json
machinist repo remove /absolute/path/to/repository
```

Portfolio status reads locally persisted legacy issue-run Evidence, isolates
per-repository errors, and does not include the foreground Task namespace.

## Recover a failed GitHub issue Task

**New in 0.15.0:** new remote Workshops fetch the intended base
branch explicitly and pin its resulting commit. A deleted or renamed remote
base fails before Workshop creation even if a stale tracking ref survives.
Check origin and the repository's current default branch, then use the normal
explicit retry path. Existing remote Task branches remain the recovery
authority; this does not replace approved heads with a newer default-branch
commit. Foreground local Workshops continue to use local commits without a
remote fetch.

1. Stop or let the current watcher pass finish.
2. Inspect the Task Run error with `machinist inspect <issue>` or `machinist status -v`.
3. Inspect any retained workspace before choosing whether to preserve its edits.
4. Fix authentication, configuration, tests, or the issue/spec as appropriate.
5. A failed Review remains draft; after fixing the adapter or output problem:

   ```sh
   machinist retry 42 --phase review
   machinist review 42
   ```

6. To continue the retained Execute workspace, run from the repository root:

   ```sh
   machinist retry 42 --phase execute --run --resume
   ```

7. To leave the failed workspace behind and provision a clean Execute attempt,
   use `--fresh`:

   ```sh
   machinist retry 42 --phase execute --run --fresh
   ```

   Fresh is the default when neither `--resume` nor `--fresh` is supplied.
8. Restart a long-running watcher (`machinist watch`) only after the explicit
   retry completes or the Task Run is marked retryable.

Do not edit Task Run JSON by hand. `--resume` validates the managed workspace
against the recorded branch and head before reusing it. `--fresh` starts from
the approved head without treating diagnostic edits as implementation input.

A run that fails with `implementation deleted test file(s)` hit the
test-deletion guard. Read the retained workspace's diff first: if the harness
deleted tests to get past the gate, retry fresh; if the approved Spec
legitimately removes or renames tests, set
`limits.allow_test_deletions: true`, retry, and turn the setting back off
afterwards.

A run that fails with `controller-owned Git metadata changed during an
untrusted phase` hit the Git metadata custody guard. The message names the file
and, for a config file, the exact keys that moved. Under
`workspace.strategy: worktree` that file is usually your own repository's,
because a worktree shares `config`, `hooks/`, and `info/` with its parent, so
check first whether you installed a hook or changed Git config while the Task
was running. If you did, retry the Task and keep your own Git edits out of the
harness window, or set `workspace.strategy: clone` so each Workshop owns its
Git metadata. If you did not, treat the named keys as a custody incident:
inspect the retained workspace, revert the metadata, and rotate any credential
the changed keys could have reached before retrying.

## Cancel or amend a GitHub issue Task

To cooperatively terminate an active supervised harness/gate process and block
future watcher dispatches for the issue:

```sh
machinist cancel 42 --reason "requirements changed"
```

The cancellation marker is durable. Inspect the cancelled run and resolve the
reason first. An explicit retry marks the run retryable and clears the marker:

```sh
machinist retry 42 --phase execute --run --fresh
```

Use `machinist cancel 42 --clear` when you only want to remove the marker
without marking or running a Task retry.

For review feedback on a ready PR, approve its current head again and run one
fresh amendment:

```sh
machinist approve --issue 42
machinist inspect 42 --json
```

Wait for the managed approval workflow to succeed. Verify the PR's approval
label is present and repeat inspection until its full `approval_sha` matches
`head_sha`. A ready PR remains `in review` in legacy status even after fresh
Approval, so do not wait for an `approved` status. Then run:

```sh
machinist amend 42 --feedback-file review-notes.txt
```

Exactly one of `--feedback` or `--feedback-file` is required. Amendment does
not resume a retained failed Workshop or create a new Spec. A new successful
Execute SHA can receive a fresh independent Review and findings; same-head
successful Review is not repeated. When `review.enabled: true`, run
`machinist review 42` after the amendment succeeds, or let the watcher dispatch
it. This differs from local `amend --task`,
which generates a new Spec and requires its Approval before Execute.

## Diagnostic output

**New in 0.15.0:** controller Git, `gh`, `glab`, and doctor
diagnostics redact recognized URL credentials, authorization values, and
secret assignments, remove unsafe terminal controls, and bound rendered text.
A truncation notice means the displayed diagnostic is incomplete. This is not
an arbitrary-secret guarantee or a sanitizer for stored raw logs; inspect
Evidence and logs before sharing them.

## Configuration operations

These commands default to root `machinist.yaml`. Use read-only config
inspection before changing the GitHub watcher:

```sh
machinist config validate
machinist config show
machinist config schema --output machinist.schema.json
```

`machinist config set <dotted-key> <yaml-value>` atomically rewrites the
validated config as canonical YAML and normalizes comments. Phase-specific
harness profiles, instruction overlays, named verification gates, the harness
verification feedback loop, independent Review, telemetry, the test-deletion
guard, notifications, admission budgets, and change limits are documented in the
[getting-started reference](getting-started.md).

## GitHub Approval incidents

- `approval pending`: inspect the managed approval workflow before requesting
  approval again. The label is visible, but trusted evidence for the current
  SHA is not. Retry with `machinist approve --pr <pr>` (or
  `machinist approve --issue <issue>`) only if the workflow failed.
- `approval stale`: reread the changed spec, then approve the current head.
- Unexpected manual label: remove it, review repository workflow permissions,
  and inspect PR events. The label alone cannot authorize execution, and the
  managed workflow refuses to mint evidence unless the labeling actor has
  write or admin access.
- Approval evidence records who approved. Read the approval comment on the PR
  to see the login the workflow bound the SHA for. If that login is not who
  you expected, revoke their access before retrying anything.
- Existing installs must run `machinist sync-workflows` to pick up the actor
  check; `machinist doctor` reports the drift until they do.

## Legacy Workshop cleanup

These commands select legacy issue Workshops, not foreground local Workshops.
Inspect before deleting. You can list and prune managed workspaces directly:

```sh
machinist clean
machinist clean --issue 42
machinist clean --all
```

Or manage worktrees manually from the source checkout:

```sh
git worktree list
git worktree remove /absolute/path/to/workspace
git worktree prune
```

Use `--force` only after confirming no useful uncommitted diagnosis remains.
Clone-strategy workspaces are ordinary directories but deserve the same check.

## GitHub workflow changes

After editing dispatcher ownership or labels:

```sh
machinist sync-workflows
machinist task template --write
git diff -- .github/workflows
git diff -- .github/ISSUE_TEMPLATE/agentmachinist-task.yml
uv run pytest
```

Commit and push the reviewed projections. Do not hand-maintain managed workflow
or task-template content; ownership markers make modified files fail closed.

## Release checklist

1. Update `CHANGELOG.md` and `pyproject.toml` to the same version.
2. Run `uv lock` and `bash scripts/verify.sh`; this runs tests, checks managed
   workflows, builds both distributions, installs the wheel and sdist, and
   exercises a generated first-run project.
3. Run `machinist sync-workflows --check` (after a version bump, run `machinist sync-workflows`, review, and commit the projection).
4. Commit and push, then verify CI for that exact commit.
5. Create GitHub Release `v<version>`. The release workflow repeats verification,
   stages SHA-256 checksums, and publishes the verified artifacts through the
   minimal Trusted Publishing job.
6. After publish succeeds, verify that the wheel, sdist, and `SHA256SUMS` are
   attached to the GitHub Release and that their hashes match.
7. Verify the exact PyPI version exists and can run in isolation, for example
   `uv tool run --isolated --no-cache --from "agentmachinist==<version>" machinist --version`.

A local build, pushed commit, published PyPI version, attached release assets,
checksum match, and exact-version installation are separate proof states.

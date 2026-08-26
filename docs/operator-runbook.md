# Operator runbook

## Preflight

Run from the configured repository root:

```sh
machinist doctor
machinist sync-workflows --check
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
warning that no verification gates are configured is acceptable. A Task Runs
warning points to a failed or process-abandoned record that should be inspected
and explicitly retried.

## Run modes

- Interactive: `machinist watch`
- One scheduler-friendly pass: `machinist watch --once`
- Read-only admission preview: `machinist watch --dry-run`
- Manual phases: `machinist spec <issue>` and `machinist run <issue>`

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
logs. `status` reports `loaded/scheduled` because the one-shot watcher is
normally idle between intervals; that is not a claim that a process is always
running. `start`, `restart`, `stop`, `status`, `logs`, and `uninstall` remain
available if `machinist.yaml` is missing or invalid or the installed controller
is no longer on the current `PATH`. Only `install` needs a valid current config
and executable. `uninstall` removes the managed plist but preserves logs. For
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

## Admission control

The configured `queue.max_tasks_per_pass` limits each poll; the
`watch --max-tasks <n>` option overrides it for one process. `watch --dry-run`
reports eligible and deferred Tasks without claiming or dispatching them.
Optional allowed hours and daily Task/runtime budgets are evaluated from local
time and Task Run history.

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

## Observe

`machinist status` shows `awaiting spec`, `awaiting approval`,
`approval pending`, `approval stale`, `approved`, and `in review`. When a local
Task Run is active or needs intervention, its `spec running`, `execute failed`,
or `execute retryable` state overrides the GitHub eligibility state.

Local Task Run records are under `.machinist/runs/`. They are runtime state and
should remain ignored by Git. `machinist init` idempotently adds
`/.machinist/runs/` to `.gitignore`, and `doctor` reports a failure if the rule
is removed. Failed workspaces are retained by the default `cleanup: on_success`
policy; the error prints their path.

For complete or scriptable local evidence, use:

```sh
machinist status --local --json
machinist runs --issue 42 --json
machinist inspect 42 --offline --json
```

The local read model includes current/history records plus orphaned, partial,
and corrupt artifacts. Without `--offline`, inspection adds GitHub sources but
still preserves readable local evidence when a remote source fails.

For a solo portfolio, register canonical repository roots and read them without
changing directories:

```sh
machinist repo add /absolute/path/to/repository
machinist repo list
machinist status --all --json
machinist repo remove /absolute/path/to/repository
```

Portfolio status is deliberately local-only and isolates per-repository errors.

## Recover a failed task

1. Stop or let the current watcher pass finish.
2. Inspect the Task Run error with `machinist inspect <issue>` or `machinist status -v`.
3. Inspect any retained workspace before choosing whether to preserve its edits.
4. Fix authentication, configuration, tests, or the issue/spec as appropriate.
5. To continue the retained Execute workspace, run from the repository root:

   ```sh
   machinist retry 42 --phase execute --run --resume
   ```

6. To leave the failed workspace behind and provision a clean Execute attempt,
   use `--fresh`:

   ```sh
   machinist retry 42 --phase execute --run --fresh
   ```

   Fresh is the default when neither `--resume` nor `--fresh` is supplied.
7. Restart a long-running watcher (`machinist watch`) only after the explicit
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

## Cancel or amend a task

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
machinist amend 42 --feedback-file review-notes.txt
```

Exactly one of `--feedback` or `--feedback-file` is required. Amendment does
not resume a retained failed workspace.

## Configuration operations

Use read-only config inspection before changing the watcher:

```sh
machinist config validate
machinist config show
machinist config schema --output machinist.schema.json
```

`machinist config set <dotted-key> <yaml-value>` atomically rewrites the
validated config as canonical YAML and normalizes comments. Phase-specific
harness profiles, instruction overlays, named verification gates, the harness
verification feedback loop, the test-deletion guard, notifications, admission
budgets, and change limits are documented in the
[getting-started reference](getting-started.md).

## Approval incidents

- `approval pending`: rerun `machinist approve --pr <pr>` (or `machinist approve --issue <issue>`) or post the exact comment.
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

## Workspace cleanup

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

## Workflow changes

After editing dispatcher ownership or labels:

```sh
machinist sync-workflows
git diff -- .github/workflows
uv run pytest
```

Commit and push the reviewed projection. Do not hand-maintain managed workflow
labels; the next sync intentionally replaces drift.

## Release checklist

1. Update `CHANGELOG.md` and `pyproject.toml` to the same version.
2. Run `uv lock` and `bash scripts/verify.sh`; this runs tests, checks managed
   workflows, builds both distributions, and smoke-tests the wheel.
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

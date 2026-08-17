# Operator runbook

## Preflight

Run from the configured repository root:

```sh
machinist doctor
machinist sync-workflows --check
machinist status
```

Resolve `FAIL` checks before unattended work. Decide explicitly whether a
`tests.command: null` warning is acceptable. A Task Runs warning points to a
failed or process-abandoned record that should be inspected and explicitly
retried.

## Run modes

- Interactive: `machinist watch`
- One scheduler-friendly pass: `machinist watch --once`
- Manual phases: `machinist spec <issue>` and `machinist run <issue>`

Run one local watcher per repository. The claim is local, not cross-host. If
`github.spec_source` is `github-actions`, the local watcher handles approved
execution but deliberately skips spec generation.

For launchd or cron, set the working directory to the repository root and use
absolute paths to `machinist` if the scheduler has a restricted `PATH`. Capture
stdout and stderr in a rotating log. `watch --once` is easier to supervise than
a permanently attached process.

## Observe

`machinist status` shows `awaiting spec`, `awaiting approval`,
`approval pending`, `approval stale`, `approved`, and `in review`. When a local
Task Run is active or needs intervention, its `spec running`, `execute failed`,
or `execute retryable` state overrides the GitHub eligibility state.

Local Task Run records are under `.machinist/runs/`. They are runtime state and
should remain ignored by Git. Failed workspaces are retained by the default
`cleanup: on_success` policy; the error prints their path.

## Recover a failed task

1. Stop or let the current watcher pass finish.
2. Read the Task Run error and inspect the retained workspace without deleting
   it.
3. Fix authentication, configuration, tests, or the issue/spec as appropriate.
4. Mark one phase retryable:

   ```sh
   machinist retry 42 --phase execute
   ```

5. Restart a long-running watcher or run the phase manually.

Do not edit Task Run JSON by hand. A crash after push is reconciled using its
checkpoint; tests run again and the harness does not.

## Approval incidents

- `approval pending`: rerun `machinist approve <pr>` or post the exact comment.
- `approval stale`: reread the changed spec, then approve the current head.
- Unexpected manual label: remove it, review repository workflow permissions,
  and inspect PR events. The label alone cannot authorize execution.

## Workspace cleanup

Inspect before deleting. For worktrees, from the source checkout:

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

1. Update `CHANGELOG.md` and `pyproject.toml`.
2. Run `uv lock`, `uv run pytest`, and `uv build`.
3. Install the wheel in an isolated environment and run `machinist --version`.
4. Commit and push.
5. Create GitHub Release `v<version>`.
6. Separately verify the Actions job and the published PyPI artifact.

A local build, pushed commit, successful release workflow, and published PyPI
artifact are separate proof states.

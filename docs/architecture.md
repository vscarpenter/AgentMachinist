# Architecture and lifecycle

AgentMachinist is a controller around four external systems: Git, GitHub, a
coding harness, and the repository's test command. Its useful boundary is a
ready-for-review pull request—not merge or deployment.

## Ownership

| Owner | Responsibilities |
| --- | --- |
| GitHub | Issues, PRs, branch heads, approval marker comments, labels. |
| AgentMachinist | Eligibility, local claims, Task Runs, workspaces, commits, leased pushes, PR readiness. |
| Harness | Read repository context and return a spec or working-tree edits; may pre-run the configured verification gates to iterate. |
| Human | Issue intent, spec approval, code review, merge. |

The controller keeps Git authority. Prompts tell the harness not to use Git;
postconditions detect commits, remote branch changes, and `.machinist/` edits
before the controller proceeds.

## Lifecycle

```text
trigger label
    │
    ▼
awaiting spec ── SPEC succeeds ──► awaiting approval
                                         │ label without evidence
                                         ▼
                                  approval pending
                                         │ SHA marker matches head
                                         ▼
                                      approved
                                         │ branch changes
                                         ▼
                                   approval stale

approved ── EXECUTE + test gate succeeds ──► in review
```

Non-draft PR state outranks a leftover approval label, so a completed PR cannot
be reclassified as executable.

## Immutable approval

Approval evidence is an HTML comment marker:

```text
<!-- agentmachinist:approval sha=<40-hex-head-sha> -->
```

The workflow records the marker before adding the label. Execution requires
both the label and a marker matching the current PR head. Manual label events
also stamp the head. A later branch update naturally invalidates approval.

## Claims, Task Runs, and recovery

`.machinist/runs/issue-<n>-<phase>.json` is the atomically written current-state
projection. Each attempt also has append-only JSONL history under
`.machinist/runs/history/`; controller checkpoints record intent before remote
effects and the observed result afterward. Records include the phase, attempt,
timestamps, status, error, harness profile, duration, deviations, and
reconciliation evidence. A local `flock` plus an in-process guard prevents
overlapping work for one issue on a single host. It is not a distributed
GitHub claim.

Task Run states are `running`, `succeeded`, `failed`, `retryable`, `cancelled`,
and `abandoned`. Explicit recovery moves an interrupted or unsuccessful record
to `retryable`; `machinist retry` is the canonical operator command. Checkpoints
survive a crash and preserve the approved SHA and pushed implementation SHA as
recovery evidence. Execute recovery with
`machinist retry <issue> --phase execute --run --resume` validates and reuses
the retained managed workspace; the same command with `--fresh` provisions
another attempt from the approved head. Fresh is the default when neither
recovery flag is supplied.

Cancellation requests and queue controls are separate durable admission
records. `machinist cancel` cooperatively stops supervised process groups and
blocks a later watcher dispatch until cleared. Queue pause/defer controls only
new admissions; it does not interrupt an active claim. Malformed control state
fails closed rather than silently admitting work.

A successful Spec is regenerated only through the `--revise` mode of
`machinist spec <issue>`, which updates its existing branch and draft PR. The
`--abandon` mode records rejection, removes lifecycle labels, and closes an open
draft PR without merging it.

A ready PR can be reworked with `machinist amend <issue> --feedback ...` only
after its current head is approved again. Amend always starts from the approved
remote head. Explicit feedback is bounded and recorded with Execute evidence.

## Verification and process supervision

Execute resolves either ordered `verification.gates` or the legacy
`tests.command` into one verification engine. By default the implement prompt
lists those gate commands and asks the harness to run required gates and
iterate until they pass before finishing; the `claude-code` adapter allowlists
exactly those commands, and `verification.harness_may_run_gates: false`
withholds both. The controller's own gate run afterwards remains the
authoritative check. Required command failures prevent
readiness; ordinary advisory command failures remain evidence. Cancellation,
forbidden mutation, or inability to snapshot the working tree always blocks
fail-closed, even for an advisory gate. Before commit or push, the controller
also enforces configured file-count, byte-count, denied-path, and binary-file
limits, and refuses deleted test files (heuristic path patterns; renames
count) unless `limits.allow_test_deletions` is set.

Harnesses and gates run under a process supervisor with bounded output,
timeouts, credential reduction, process-group termination, and cooperative
cancellation. This makes ordinary child-process failures containable; it does
not turn a local harness into a sandbox.

Phase-specific harness profiles and repository-local instruction overlays are
resolved before invocation. Instruction files must remain within the canonical
repository root and pass file-count, encoding, and byte limits.

## Dispatcher ownership

`github.spec_source` prevents local `watch` and GitHub Actions from both
claiming Phase 1. `sync-workflows` deterministically projects config and the
installed package version into managed workflow files; `--check` and `doctor`
report drift without writing.

Watcher admission combines durable queue pause/deferral state, optional allowed
hours, optional daily Task/runtime budgets, and a per-pass maximum. The
`watch --dry-run` command evaluates these controls and reports eligibility
without claiming or dispatching a Task.

The optional repository registry contains canonical local roots only.
`status --all` reads each repository's local Task Run evidence independently;
one missing or corrupt repository does not erase healthy repository results.

On macOS, the managed service is one per-repository LaunchAgent. It schedules
`machinist watch --once`, sets the repository working directory, uses an
absolute controller executable, and retains stdout/stderr under
`.machinist/runs/service/`. Stop preserves the plist; uninstall removes only the
managed plist and retains logs. A locked, atomically replaced, bounded ledger
under `.machinist/runs` deduplicates successful notification deliveries across
one-shot watcher processes for 24 hours; delivery failures are never recorded.

When `github.spec_source` is `github-actions`, `github.spec_install` is `pypi`
or `checkout`. Consumer repositories should keep `pypi`. This project's
dogfood config uses `checkout` so Spec Task Runs exercise the commit under
test.

## Push safety

Implementation pushes use `--force-with-lease` against the approved SHA. If the
remote spec branch changes while the harness works, the push fails instead of
overwriting the new head. AgentMachinist then retains the failed workspace and
Task Run for diagnosis.

GitHub credentials are scoped to controller-owned network subprocesses only:
clone, fetch, `ls-remote`, and push. Managed workflows check out with
persisted Git credentials disabled, and the controller never exposes its token
to coding harnesses or verification gates.

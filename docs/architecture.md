# Architecture and lifecycle

AgentMachinist is a controller around four external systems: Git, GitHub, a
coding Harness, and the repository's configured verification commands. Its
useful boundary is a ready-for-review pull request—not merge or deployment.

## Ownership

| Owner | Responsibilities |
| --- | --- |
| GitHub | Issues, PRs, branch heads, approval marker comments, labels. |
| AgentMachinist | Eligibility, local Claims, Task Runs, Workshops, commits, leased pushes, PR readiness. |
| Harness | Read repository context, return a spec or working-tree edits, and independently review the delivered diff; may pre-run configured verification gates to iterate. |
| Human | Issue intent, spec approval, code review, merge. |

The controller keeps Git authority. Prompts tell the harness not to use Git;
postconditions detect commits, remote branch changes, and `.machinist/` edits
before the controller proceeds.

## Deep policy seams

The controller keeps one authoritative implementation for each policy that can
change custody, spend Harness time, or interpret durable state:

| Policy | Owner | Interface |
| --- | --- | --- |
| Known Task Run Evidence | `evidence.py` | Typed reads plus Phase-aware validation for new checkpoints; persisted mappings stay open for historical compatibility. |
| Claims, journals, and inventory | `lifecycle.py` | Current projections, append-only attempts, orphan classification, and corrupt-artifact meaning. Callers do not parse journal paths. |
| Phase Task Run construction | `dispatch.py` | The only wiring point for Claims, Harnesses, Workshops, cancellation, verification, and Spec/Execute/Review functions. |
| Pipeline transitions | `transitions.py` | State vocabulary, priority, dispatch eligibility, Task Run disposition, and next action. |
| Repository and PR custody | `repository_custody.py` | One bound origin host/repository and exact same-repository PR identity checks. |
| Verification Gates | `verification.py` | The sole required/advisory, timeout, cancellation, mutation, logging, and result implementation. |
| Configuration behavior | `config.py` | Validated starter and effective projections; terminal rendering and atomic persistence live in `config_cli.py`. |

These are internal module seams, not persistence migrations. Version-1 Task Run
records and `machinist.yaml` remain compatible, and CLI text, JSON, and GitHub
effects keep their existing contracts.

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

approved ── EXECUTE + test gate succeeds ──► awaiting review
                                                    │ read-only Review succeeds
                                                    ▼
                                                in review
```

Non-draft PR state outranks a leftover approval label, so a completed PR cannot
be reclassified as executable.

Review is a first-class `Phase.REVIEW`, not a callback inside Execute. Execute
records the exact delivered SHA and leaves the PR draft. Review provisions a
clean read-only view of that head, evaluates the approved Spec, diff, and gate
evidence, posts a bounded structured report, rechecks the head, and alone marks
it ready. Findings are advisory; parse failure, mutation, cancellation, or head
drift fails the Phase without an autonomous repair loop.

## Immutable approval

Approval evidence is an HTML comment marker:

```text
<!-- agentmachinist:approval sha=<40-hex-head-sha> -->
```

The workflow records the marker before adding the label. Execution requires
both the label and a marker matching the current PR head. A later branch
update naturally invalidates approval.

Both authorization paths check the actor before any evidence is minted. A
`/machinist-execute` comment is considered only from OWNER, MEMBER, or
COLLABORATOR, and both comment and label paths independently require write or
admin access. GitHub association and label permissions can be weaker than push
authority, so neither is sufficient by itself. The permission check fails
closed: a permission that cannot be read mints no evidence. The approver's
login is recorded alongside the marker, so the comment reads:

```text
Approved by @<login> for `<40-hex-head-sha>`. <!-- agentmachinist:approval sha=<40-hex-head-sha> -->
```

The controller matches the marker anywhere in a comment authored by the
workflow (`github-actions[bot]`). A marker typed by a human is ignored whatever
their association, so the recorded approver is human-readable context rather
than part of the parsed contract.

## Claims, Task Runs, and recovery

`.machinist/runs/issue-<n>-<phase>.json` is the atomically written current-state
projection. Each attempt also has append-only JSONL history under
`.machinist/runs/history/`; controller checkpoints record intent before remote
effects and the observed result afterward. Records include the phase, attempt,
timestamps, status, error, harness profile, duration, current named stage,
progress heartbeat, deviations, and reconciliation evidence. A local `flock`
plus an in-process guard prevents overlapping work for one issue on a single
host. It is not a distributed GitHub claim.

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

## Adapter boundary

Built-ins and third-party Harnesses share the versioned
`agentmachinist.harnesses.v1` entry-point contract. Discovery validates adapter
identity, reserves built-in names, and isolates import failures. Descriptors
declare supported phases, structured-usage support, and optional hosted-Spec
CI metadata. Managed workflow projection consumes that metadata instead of
embedding a provider assumption in the controller.

Plugins are trusted local code. The contract improves discoverability and
diagnostics; it is not a sandbox or permission boundary.

## Reporting boundary

Local JSONL history is reduced to aggregate outcomes, phase/status series,
duration percentiles, gate-failure statuses, safe Harness/model identity, and
declared structured token counts. Network export lives in a separate module
that accepts only the aggregate report. Its OTLP/HTTP JSON projector constructs
an allowlist of repository, phase, status, Harness, and model attributes; it
cannot serialize issue bodies, prompts, diffs, commands, errors, environment
values, or arbitrary Evidence. Export is disabled without explicit config or a
command flag.

## Dispatch sources and admission

Every local claimed Phase enters through `TaskDispatcher`. Click commands keep
argument validation, output, notifications, and daemon presentation; watcher,
retry-now, amendment, and direct Spec/Execute/Review paths share the same Task
Run construction.

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
`.machinist/runs/service/`. Each completed pass atomically records a heartbeat
used by `service status`; lifecycle actions refuse an active Task Claim unless
the operator explicitly forces termination. Stop preserves the plist;
uninstall removes only the managed plist and retains logs. A locked, atomically
replaced, bounded ledger under `.machinist/runs` deduplicates successful
notification deliveries across one-shot watcher processes for 24 hours;
delivery failures are never recorded.

When `github.spec_source` is `github-actions`, `github.spec_install` is `pypi`
or `checkout`. Consumer repositories should keep `pypi`. This project's
dogfood config uses `checkout` so Spec Task Runs exercise the commit under
test.

## Git metadata custody

Git metadata is executable. A planted `core.fsmonitor`, clean filter, or hook
runs the next time the controller invokes Git, so the Workshop's metadata is
fingerprinted before an untrusted phase and re-checked before every later Git
call. The check reads the filesystem directly, ahead of the first Git
subprocess, so hostile metadata never gets a process to execute in.

Fingerprinted: the `.git` pointer and `commondir`, config files, controller
markers, `info/attributes`, `info/exclude`, `info/grafts`,
`objects/info/alternates`, `shallow`, `refs/replace`, and every hook.

**A worktree Workshop shares metadata with your own repository.** A Git
worktree gets its own `HEAD`, index, and refs; `config`, `hooks/`, `info/`,
and `objects/` belong to the parent. So under `workspace.strategy: worktree`
the watched config is the one you edit yourself. Under
`workspace.strategy: clone` the Workshop owns all of it.

That distinction sets how each file is compared:

| Metadata | Comparison |
| --- | --- |
| Config files shared with your repository | By sensitive key |
| Config files the Workshop owns | Byte for byte |
| Hooks, `info/`, `objects/`, `refs/replace` | Byte for byte |

A shared config file trips the guard only when a change touches a key that can
execute a program, name a path Git will trust, or redirect the network.
Editing `diff.tool` or adding a second remote does not. Planting
`core.fsmonitor`, a clean filter, an alias, a credential helper, a
`url.*.insteadOf` rewrite, an `include.path`, or a new `remote.origin.url`
does. Hooks have no benign subset, so they stay byte-compared even when
shared.

Classification runs on a parser that refuses to guess. Anything it cannot read
with confidence falls back to byte comparison, and an unreadable config file is
a custody failure rather than an assumed-benign edit. Task Run records store
hashes of sensitive config values rather than the values, keeping a
credentialed origin or an `http.*.extraheader` out of run records and error
messages.

See [the trust model](trust-model.md) for the full key list and
[the operator runbook](operator-runbook.md) for recovery.

## Push safety

Implementation pushes use `--force-with-lease` against the approved SHA. If the
remote spec branch changes while the harness works, the push fails instead of
overwriting the new head. AgentMachinist then retains the failed workspace and
Task Run for diagnosis.

GitHub credentials are scoped to controller-owned network subprocesses only:
clone, fetch, `ls-remote`, and push. Managed workflows check out with
persisted Git credentials disabled, and the controller never exposes its token
to coding harnesses or verification gates.

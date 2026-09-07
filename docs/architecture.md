# Architecture and lifecycle

AgentMachinist coordinates Git, a coding Harness, and the repository's
verification commands. A reviewed local candidate is the primary result.
GitHub/GitLab intake and publication are optional; integration requires an
explicit human command and a clean fast-forward. Remote merge and production
deployment remain outside the controller. AgentMachinist 0.14.0 includes the
foreground local workflow and GitLab intake/publication alongside the compatible
GitHub issue workflow.

## Ownership

| Owner | Responsibilities |
| --- | --- |
| GitHub/GitLab | Optional source issues and published PRs/MRs; GitHub additionally supports its legacy trusted Approval workflows. |
| AgentMachinist | Local Task identity, Approval, Claims, Task Runs, Workshops, commits, verified candidates, explicit local integration, leased publication. |
| Harness | Read repository context, return a spec or working-tree edits, and independently review the delivered diff; may pre-run configured verification gates to iterate. |
| Human | Task intent, exact Spec Approval, code review, explicit local integration or remote merge. |

The controller keeps Git authority. Prompts tell the Harness not to use Git.
Both workflows check Workshop/controller Git custody, commits, and protected
`.machinist/` content before proceeding. Local Tasks also check local refs;
legacy GitHub Phases check live remote-head postconditions. Foreground local
Phases make no remote-head query before optional publication.

## Local lifecycle and optional publication

`local_tasks.py` stores monotonic local IDs (`T1`), revision-checked records,
operation Claims, and reports under `.machinist/runs/local/`. Imported issues
are provenance; they do not determine local identity or grant Approval.
`local_setup.py` supplies minimal runtime settings without forge setup. First
start copies applicable root configuration into
`.machinist/runs/local/config.yaml`, then enforces required verification and
Review, local Spec source, managed workflows off, telemetry endpoint unset,
and an absolute Workshop root outside the repository. Later local commands
read the saved copy; root changes do not propagate automatically. Runtime
exclusion is local Git metadata, so adoption need not edit tracked files.

`local_workflow.py` sends every Spec, Execute, and Review Task Run through
`dispatch.py`, sharing the existing lifecycle, Evidence, Verification, and
cancellation policies. Human Approval names the exact repository, Task, and
Spec commit. Baseline Verification Gates run before Spec generation; the Gate
command must prepare dependencies absent from the committed Workshop. Local
Review always runs and remains advisory. A new Spec
invalidates Approval; a new successful Execute SHA permits a fresh Review.

`local_workspace.py` provisions Workshops from local commits without contacting
a forge and retains controller-owned candidate refs before cleanup. Worktree
Workshops share their parent repository's configuration and remotes; isolated
clones have their copy-source `origin` removed. Explicit integration records
intent, checks the clean base/candidate identities and ancestry, and advances
only by fast-forward. The operation is complete only after the observed base
and checkout match the exact candidate; saved intent alone is not success.
An interrupted operation reconciles that recorded result. The starting named
branch and commit define the integration base; dirty, changed-base,
changed-candidate, and non-fast-forward states fail. A local amendment requires
and starts from the previous completed reviewed candidate, invalidates Approval,
and creates a new Spec. It is disallowed once integration intent has been saved,
including when a later integration precondition fails.

`publication.py` consumes the completed local candidate independently from the
machine Phases. It checks exact successful Execute and Review Evidence, binds
the origin to `forge.py`/`gitlab.py`, and journals intended SHA and remote lease
before pushing. PR/MR creation and retries preserve number, repository, branch,
base, open state, and exact head. A saved `publishing` stage records an attempt;
`published` is recorded only after the remote branch and PR/MR both match the
candidate. Publication failures leave the local candidate and Evidence intact.
`forge.py` supplies the GitHub adapter and normalized
publication contract; `gitlab.py` uses host-bound `glab` calls, including nested
project paths and self-managed hosts. Imported issue numbers remain external
provenance. They do not select publication origin or supply local Approval.
Native GitLab Spec CI, GitLab remote Approval, and remote merge are out of scope.
See [ADR 0003](adr/0003-local-workflow-and-optional-publication.md).

## Deep policy seams

**Unreleased / source checkout:** `local_doctor.py` adds optional
`machinist doctor --local` using the read-only `local_setup.py` configuration
resolver shared with `start`. Saved local settings take precedence; first-run
discovery is previewed without adoption. It combines local Git/author/checkout
checks with the existing Harness probes and Gate command checks, returning the
existing doctor report contract. It creates no Task, Claim, Workshop, runtime
file, configuration, exclusion, or ref and makes no model, forge, or update
request by default. Explicit `--run-gates` reuses `verification.py` in the
controller checkout, where commands may write or download; this does not prove
the isolated baseline. Plain `doctor` keeps its GitHub setup behavior. These
additions are not included in the published 0.14.0 package.

The controller keeps one authoritative implementation for each policy that can
change custody, spend Harness time, or interpret durable state:

| Policy | Owner | Interface |
| --- | --- | --- |
| Known Task Run Evidence | `evidence.py` | Typed reads plus Phase-aware validation for new checkpoints; persisted mappings stay open for historical compatibility. |
| Claims, journals, and inventory | `lifecycle.py` | Current projections, append-only attempts, orphan classification, and corrupt-artifact meaning. Callers do not parse journal paths. |
| Phase Task Run construction | `dispatch.py` | The only wiring point for Claims, Harnesses, Workshops, cancellation, verification, and Spec/Execute/Review functions. |
| Pipeline transitions | `transitions.py` | State vocabulary, priority, dispatch eligibility, Task Run disposition, and next action. |
| Repository and change custody | `repository_custody.py` for legacy GitHub; `local_workspace.py`, `publication.py`, and `forge.py` for optional delivery | Bind origin host/repository and verify exact PR/MR identity and candidate SHA. |
| Verification Gates | `verification.py` | The sole required/advisory, timeout, cancellation, mutation, logging, and result implementation. |
| Configuration behavior | `config.py` | Validated starter and effective projections; terminal rendering and atomic persistence live in `config_cli.py`. |

These are internal module seams, not persistence migrations. Version-1 Task Run
records and `machinist.yaml` remain compatible, and CLI text, JSON, and GitHub
effects keep their existing contracts.

## Legacy GitHub lifecycle

With `review.enabled: true`:

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

Review is a first-class `Phase.REVIEW`, not a callback inside Execute. When
enabled for the legacy GitHub workflow, Execute
records the exact delivered SHA and leaves the PR draft. Review provisions a
clean read-only view of that head, evaluates the approved Spec, diff, and gate
evidence, posts a bounded structured report, rechecks the head, and alone marks
it ready. Findings are advisory; parse failure, mutation, cancellation, or head
drift fails the Phase without an autonomous repair loop. With legacy
`review.enabled: false`, Execute marks the PR ready after its configured Gates;
there is no independent Review guarantee. Local Review cannot be disabled.

## Legacy GitHub immutable Approval

Approval evidence is an HTML comment marker:

```text
<!-- agentmachinist:approval sha=<40-hex-head-sha> -->
```

For a comment request, the workflow records the marker before adding the
label. For a label-triggered request, the label is already present while the
workflow verifies the actor and records the marker. Execution requires both
the label and a trusted marker matching the current PR head; the label alone
is an `approval pending` state. A later branch update naturally invalidates
Approval.

Legacy `machinist approve --issue <n>` or `--pr <n>` posts the request and
returns before this GitHub workflow completes. Wait for the workflow to
succeed and confirm the matching marker and label before starting Execute or
amendment. Starting Execute too soon fails its Approval guard and requires an
explicit retry. Foreground `approve --task T1 --spec-sha <sha>` records local
Approval synchronously and then continues Execute and Review.

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

Legacy `.machinist/runs/issue-<n>-<phase>.json` is the atomically written
current-state projection. Foreground Tasks use the same lifecycle format inside
`.machinist/runs/local/`; the integer in these internal Phase filenames is the
local Task number, not a GitHub issue ID. Local Task records and reports live
in `.machinist/runs/local/tasks/`, with revision-checked atomic writes. Each
attempt also has append-only JSONL history under
`.machinist/runs/history/` for legacy issues or
`.machinist/runs/local/history/` for foreground Tasks. Controller checkpoints
record intent before side effects and the observed result afterward. Records
include the phase, attempt,
timestamps, status, error, harness profile, duration, current named stage,
progress heartbeat, deviations, and reconciliation evidence. A local `flock`
plus an in-process guard prevents overlapping Phases within a repository/run
namespace. The guard keys on the canonical runtime directory and Task number,
so `T1` and legacy issue 1 do not collide. Local Task operations also hold a
per-Task operation Claim. These Claims are not distributed coordination.

Task Run states are `running`, `succeeded`, `failed`, `retryable`, `cancelled`,
and `abandoned`. Explicit recovery moves an interrupted or unsuccessful record
to `retryable`; `machinist retry` is the canonical operator command. Checkpoints
survive a crash and preserve the approved SHA and pushed implementation SHA as
recovery evidence. Execute recovery with
`machinist retry <issue> --phase execute --run --resume` validates and reuses
the retained managed workspace; the same command with `--fresh` provisions
another attempt from the approved head. Fresh is the legacy default when
neither recovery flag is supplied. Foreground
`machinist retry --task T1 --phase execute` immediately resumes retained edits
by default; `--fresh` selects a new Workshop. Successful committed work is
reconciled from Evidence rather than repeating the Harness or successful Gates.

Cancellation requests and legacy watcher queue controls are separate durable
records. Local cancellation is namespaced under `.machinist/runs/local/` and
uses the explicit `--task` selector. `machinist cancel` cooperatively stops supervised process groups and
blocks a later watcher dispatch until cleared. Queue pause/defer controls only
new admissions; it does not interrupt an active claim. Malformed control state
fails closed rather than silently admitting work.

A successful legacy GitHub Spec is regenerated only through the `--revise` mode of
`machinist spec <issue>`, which updates its existing branch and draft PR. The
`--abandon` mode records rejection, removes lifecycle labels, and closes an open
draft PR without merging it.

A ready legacy GitHub PR can be reworked with `machinist amend <issue> --feedback ...` only
after its current head is approved again. Amend always starts from the approved
remote head. Explicit feedback is bounded and recorded with Execute Evidence.
The dispatcher permits a fresh Review after the successful Execute SHA changes
and clears stale findings/comment Evidence for that attempt. It does not repeat
a successful Review of the same SHA.

## Verification and process supervision

Execute resolves either ordered `verification.gates` or the legacy
`tests.command` into one verification engine. By default the implement prompt
lists those gate commands and asks the harness to run required gates and
iterate until they pass before finishing; the `claude-code` adapter allowlists
those commands and added-argument variants. Setting
`verification.harness_may_run_gates: false` omits the prompt instructions and
these Claude allow rules; it is not a universal command-execution restriction. The controller's own gate run afterwards remains the
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

`machinist report` reduces locally stored legacy issue JSONL history to
aggregate outcomes, phase/status series,
duration percentiles, gate-failure statuses, safe Harness/model identity, and
declared structured token counts. Network export lives in a separate module
that accepts only the aggregate report. Its OTLP/HTTP JSON projector constructs
an allowlist of repository, phase, status, Harness, and model attributes; it
cannot serialize issue bodies, prompts, diffs, commands, errors, environment
values, or arbitrary Evidence. Export is disabled without explicit config or a
command flag. It does not aggregate the nested local Task namespace. Foreground
Tasks instead expose status JSON, saved Phase Evidence, and a Markdown report;
local configuration requires telemetry export to remain disabled.

## Dispatch sources and admission

Every claimed Phase in either workflow enters through `TaskDispatcher`. Click
commands keep
argument validation, output, notifications, and daemon presentation; watcher,
retry-now, amendment, and direct Spec/Execute/Review paths share the same Task
Run construction.

`github.spec_source` prevents local `watch` and GitHub Actions from both
claiming Phase 1. `sync-workflows` deterministically projects config and the
installed package version into managed workflow files; `--check` and `doctor`
report drift without writing.

Watcher admission combines durable queue pause/deferral state, optional allowed
hours, optional daily Task Run/runtime budgets, and a per-pass maximum.
`max_runs_per_day` counts individual Phase attempts and accepts the older
`max_tasks_per_day` spelling as an input alias. These watcher controls do not
govern foreground local Tasks. The
`watch --dry-run` command evaluates these controls and reports eligibility
without claiming or dispatching a Task.

The optional repository registry contains canonical local roots only.
`status --all` reads each repository's locally stored legacy issue-run Evidence
independently; one missing or corrupt repository does not erase healthy
repository results. It does not include foreground `T1` records. A checkout with
local configuration routes default `status` to local Tasks; `runs`, `inspect`,
`explain`, plain `doctor`, `clean`, and aggregate reports retain legacy scope.
The unreleased `doctor --local` selects readiness for the local workflow.
`config` defaults to root `machinist.yaml`, with `--path` required to inspect or
change the foreground local configuration.

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
worktree gets its own `HEAD`, index, and per-worktree refs; branch refs,
`config`, `hooks/`, `info/`, and `objects/` belong to the parent. So under `workspace.strategy: worktree`
the watched config is the one you edit yourself. Under
`workspace.strategy: clone` the Workshop owns all of it.

That distinction sets how each file is compared:

| Metadata | Comparison |
| --- | --- |
| Config files shared with your repository | By sensitive key |
| Config files the Workshop owns | Byte for byte |
| Fingerprinted hooks, `info/`, object alternates, and `refs/replace` | Byte for byte |

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

**Unreleased / source checkout:** legacy `Workspace.provision` fetches the
intended remote base explicitly for new Tasks, resolves the freshly fetched
ref, and constructs the Workshop from that immutable SHA. Missing or deleted
remote bases fail even when a stale tracking ref remains; a narrow fetch
configuration cannot silently select an older local ref. An existing remote
Task branch remains the recovery authority. `LocalWorkspace` continues to
provision from committed local state without fetching a remote.

Legacy GitHub implementation pushes use `--force-with-lease` against the
approved SHA. If the remote spec branch changes while the harness works, the push fails instead of
overwriting the new head. AgentMachinist then retains the failed workspace and
Task Run for diagnosis.

Optional local publication leases against its persisted remote expectation,
refuses unowned branches, and binds exactly one origin URL without URL rewrites
or a separate push URL. GitHub/`gh` and GitLab/`glab` provide credentials for
explicit controller publication; a local Task before publication needs neither.

Controller-provided forge credentials are scoped to its network subprocesses:
clone, fetch, `ls-remote`, and push. Managed workflows check out with
persisted Git credentials disabled, and the controller never exposes its token
to coding harnesses or verification gates.

## Diagnostic rendering

**Unreleased / source checkout:** `diagnostics.py` owns bounded rendering for
Git, GitHub, GitLab, and doctor diagnostics. It redacts recognized URL userinfo,
authorization values, and secret assignments and strips unsafe terminal
controls before truncation. Exception categories and useful context remain
available to callers. This is defense in depth, not proof arbitrary output is
secret-free; it does not sanitize stored raw logs or all successful command
output. The helper reads no credential store or environment values.

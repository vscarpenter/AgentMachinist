# Architecture and lifecycle

AgentMachinist is a controller around four external systems: Git, GitHub, a
coding harness, and the repository's test command. Its useful boundary is a
ready-for-review pull request—not merge or deployment.

## Ownership

| Owner | Responsibilities |
| --- | --- |
| GitHub | Issues, PRs, branch heads, approval marker comments, labels. |
| AgentMachinist | Eligibility, local claims, Task Runs, workspaces, commits, leased pushes, PR readiness. |
| Harness | Read repository context and return a spec or working-tree edits. |
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

`.machinist/runs/issue-<n>-<phase>.json` is written atomically. It records the
phase, attempt, timestamps, status, error, and reconciliation evidence. A local
`flock` plus an in-process guard prevents overlapping work for one issue on a
single host. It is not a distributed GitHub claim.

States are `running`, `succeeded`, `failed`, and `retryable`. Only
`machinist retry` moves a failed record to retryable. Checkpoints survive a
crash. In execute, the approved SHA and pushed implementation SHA let a retry
recognize a completed push, rerun the test gate, and finish the PR transition
without invoking the harness twice.

## Dispatcher ownership

`github.spec_source` prevents local `watch` and GitHub Actions from both
claiming Phase 1. `sync-workflows` deterministically projects config and the
installed package version into managed workflow files; `--check` and `doctor`
report drift without writing.

When `github.spec_source` is `github-actions`, `github.spec_install` is `pypi`
or `checkout`. Consumer repositories should keep `pypi`. This project's
dogfood config uses `checkout` so Spec Task Runs exercise the commit under
test.

## Push safety

Implementation pushes use `--force-with-lease` against the approved SHA. If the
remote spec branch changes while the harness works, the push fails instead of
overwriting the new head. AgentMachinist then retains the failed workspace and
Task Run for diagnosis.

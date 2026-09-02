# AgentMachinist TL;DR

AgentMachinist turns one GitHub issue into a reviewed pull request:

```text
Task → Spec → approve exact SHA → Execute → verify → Review → ready PR → you merge
```

The controller owns Git and GitHub transitions. The Harness writes the Spec,
edits code, and reviews the result. AgentMachinist never merges.

## One-time setup

```sh
cd your-repository
machinist onboard                 # choose local or github-actions Spec dispatch
machinist doctor --run-gates      # resolve every FAIL before unattended work
git status --short
git add machinist.yaml .machinist/specs/.gitkeep .gitignore
git add .github/ISSUE_TEMPLATE/agentmachinist-task.yml
git add -p .github/workflows      # review generated workflows
git diff --cached
git commit -m "chore: configure AgentMachinist"
git push
```

`local` is the recommended first-run mode. If `github.spec_source` is
`github-actions`, add the selected Spec adapter's declared secret and push the
generated workflow before the first Task. CI owns only Spec; Execute and Review
still run locally. See the [Harness matrix](harnesses.md) for secret names.

## Local Spec flow

```sh
machinist task new --title "Fix login redirect" --dispatch
machinist spec <issue>             # or leave machinist watch running
# Read the draft Spec PR, then:
machinist approve --issue <issue>
machinist status                   # continue only when state is approved
machinist run <issue>              # Execute + authoritative Verification Gate
machinist review <issue>           # independent Review marks the PR ready
```

Review the ready PR and merge it yourself.

## GitHub Actions Spec flow

```sh
machinist task new --title "Fix login redirect" --dispatch
# The managed workflow writes the Spec and opens its draft PR.

# Read the draft Spec PR, then:
machinist approve --issue <issue>
machinist status                   # wait for trusted SHA Evidence

machinist run <issue>              # implementation always runs locally
machinist review <issue>           # Review also runs locally
```

You can leave `machinist watch` running instead of invoking Spec, Execute, and
Review manually. `github.spec_source` changes only who writes the Spec.

## When something stops

```sh
machinist doctor --run-gates
machinist inspect <issue>
machinist retry <issue> --phase review   # or spec / execute
```

For a failed Execute attempt, choose retained edits or a clean Workshop:

```sh
machinist retry <issue> --phase execute --run --resume
machinist retry <issue> --phase execute --run --fresh   # default
```

Revise a successful Spec with `machinist spec <issue> --revise`. Do not edit
managed workflows directly; change `machinist.yaml`, run
`machinist sync-workflows`, review the diff, commit, and push it. Approval
becomes stale whenever the Spec head changes.

For setup details, recovery cases, and trust limits, see
[Getting Started](getting-started.md), the [operator runbook](operator-runbook.md),
and the [trust model](trust-model.md).

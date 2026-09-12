# AgentMachinist TL;DR

Start with a bounded Task and return to a reviewed local change:

```text
Task → Spec → approve exact SHA → Execute → verify → Review → you integrate
                                                    → optional GitHub PR / GitLab MR
```

The controller owns Git and durable Evidence; the Harness writes the Spec, edits code, and reviews the result. AgentMachinist never merges automatically or remotely; local integration is explicit.

## One-time setup

Install AgentMachinist 0.15.0 with `uv tool install agentmachinist`, or upgrade with `uv tool upgrade agentmachinist`, then enter the repository you want to change.
Enter a clean named branch with an initial Git commit, configured author, an installed and authenticated Harness,
and an executable required Verification Gate. No forge or origin is required.

New in 0.15.0: optional `machinist doctor --local` checks readiness without adoption or model/forge calls.
`--run-gates` explicitly runs project commands in your controller checkout; they may write/download and do not prove the isolated baseline.

## First local Task

```sh
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
```

Read the saved Spec and copy its exact Approval command:

```sh
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
```

Approval continues implementation, verification, and independent Review. Use
`machinist status T1` to find the report path and exact Spec/Candidate SHAs.
Read the report and compare those commits with `git diff` before `machinist integrate T1`.

First start saves local settings in `.machinist/runs/local/config.yaml`, copying applicable root settings once.
Inspect them with `machinist config show --path .machinist/runs/local/config.yaml`.
Baseline verification runs in the isolated committed checkout before model work;
the command must prepare any dependencies absent from that Workshop. Local Review always runs.

Integration requires the clean expected base and exact reviewed candidate and
permits only fast-forward. Review findings are advisory. Local orchestration
can still use a cloud model; offline inference needs separate configuration.

## Optional collaboration

```sh
machinist start --from-issue https://gitlab.com/team/project/-/issues/42
```

Use the returned Task ID/SHA for Approval and finish Execute, verification,
and Review before publishing. For a completed Task whose ID is `T1`:

```sh
machinist publish T1 --provider gitlab
# Or: machinist publish T1 --provider github
```

Authenticate `glab` or `gh` for issue import and publication. Only publication
requires a matching origin URL; it can retry without repeating local Phases.
GitLab supports nested projects and self-managed hosts, without native Spec CI or remote Approval.

## Recovery and amendments

- Resume failed Execute: `machinist retry --task T1 --phase execute`.
- Start a new Workshop instead: add `--fresh` to that retry command.
- Rework a reviewed candidate: `machinist amend --task T1 --feedback "Also name the rejected timezone value."`

Local retry runs immediately. Amendment requires a verified, reviewed candidate and generates a Spec needing fresh Approval.
Once integration starts, use a new Task. `machinist continue T1` advances eligible
work or reports the next action; it cannot bypass Approval or explicit retry.

## Existing GitHub automation

`machinist onboard` resumes saved setup choices.
Review and commit/push manual setup changes. `machinist onboard --setup-pr`
commits and pushes managed changes and opens or resumes a draft PR; review and merge it.
Run `machinist doctor --run-gates` after setup is merged. For `github.spec_source: github-actions`, add
the selected Spec adapter's declared secret. Execute and optional Review run locally.

```sh
machinist approve --issue <issue>
```

For the first Execute on a draft Spec PR, wait for `machinist explain <issue>`
to report `approved`: the managed workflow must record the trusted exact-SHA
Evidence before execution. Then run:

```sh
machinist run <issue>
```

Only with `review.enabled: true`, follow successful Execute with `machinist review <issue>`.

With local configuration present, default `status` lists local Tasks. Plain `doctor`, `runs`, `inspect`,
`report`, `watch`, and portfolio `status --all` retain GitHub issue/configuration scope.
Local records are separate; watcher budgets do not limit foreground Tasks.

See the [local workflow](local-workflow.md), [Getting Started](getting-started.md), [operator runbook](operator-runbook.md), and [trust model](trust-model.md).

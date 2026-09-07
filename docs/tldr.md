# AgentMachinist TL;DR

Start with a bounded Task and return to a reviewed local change:

```text
Task → Spec → approve exact SHA → Execute → verify → Review → you integrate
                                                    → optional GitHub PR / GitLab MR
```

The controller owns Git and durable Evidence. The Harness produces the Spec,
edits code, and reviews the result. AgentMachinist never merges automatically
or remotely; local integration is explicit.

## One-time setup

Install AgentMachinist 0.14.0 with `uv tool install agentmachinist`, or upgrade
with `uv tool upgrade agentmachinist`, then enter the repository you want to change.
You need a clean checkout on a named branch, an initial Git commit, a
configured author, one installed and authenticated Harness, and an executable
required Verification Gate. No forge or origin is required.

## First local Task

```sh
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
# Read the saved Spec and copy its exact Approval command:
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
# Approval continues implementation, verification, and independent Review.
machinist status T1
# Inspect the report/diff, then:
machinist integrate T1
```

First start saves local settings in `.machinist/runs/local/config.yaml`,
copying applicable root settings once. Use `config show --path
.machinist/runs/local/config.yaml` to inspect them. Baseline verification runs
in the isolated committed checkout before model work; the command must also
prepare any dependencies absent from that Workshop. Local Review always runs.

Integration requires the clean expected base and exact reviewed candidate and
permits only fast-forward. Review findings are advisory. Local orchestration
can still use a cloud model; offline inference needs separate configuration.

## Optional collaboration

You can import an issue instead of typing the objective:

```sh
machinist start --from-issue https://gitlab.com/team/project/-/issues/42
```

Use the returned Task ID/SHA for Approval and finish Execute, verification,
and Review before publishing. For a completed Task whose ID is `T1`:

```sh
machinist publish T1 --provider gitlab
# Or: machinist publish T1 --provider github
```

Authenticate `glab` or `gh` for the selected host and configure one matching
origin URL for publication. Issue import alone does not require an origin.
Publication binds to origin
and can retry without repeating local Phases. GitLab supports nested projects
and self-managed hosts; it does not supply native Spec CI or remote Approval.

## Recovery and amendments

```sh
machinist retry --task T1 --phase execute
machinist retry --task T1 --phase execute --fresh
machinist amend --task T1 --feedback "Also name the rejected timezone value."
```

Local retry runs immediately and resumes Execute edits by default; `--fresh`
uses a new Workshop. Amendment requires a completed, verified and reviewed
candidate; it generates a new Spec requiring fresh Approval. After integration
starts, use a new Task. `machinist continue T1` reports or advances the next
eligible action; it does not bypass Approval or explicit retry.

## Existing GitHub automation

The existing issue/watcher workflow remains available through
`machinist onboard`, which resumes valid partial setup without overwriting
choices. `machinist onboard --setup-pr` delivers or resumes a draft setup PR.
Review, commit, push, and merge setup before running
`machinist doctor --run-gates`. For `github.spec_source: github-actions`, add
the selected Spec adapter's declared secret. Execution runs on the configured local runner:

```sh
machinist approve --issue <issue>
machinist run <issue>
machinist review <issue>
```

With local configuration present, default `status` lists local Tasks. Legacy
`doctor`, `runs`, `inspect`, `report`, `watch`, and portfolio `status --all`
retain their GitHub issue/configuration scope. Local records are separate;
watcher budgets do not limit foreground Tasks.

See the [local workflow](local-workflow.md), [Getting Started](getting-started.md), [operator runbook](operator-runbook.md), and [trust model](trust-model.md).

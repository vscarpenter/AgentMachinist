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

The new local workflow is unreleased. Install this source checkout with
`uv tool install --editable .`, then enter the repository you want to change.
You need an initial Git commit, a configured author, one installed Harness, and
an executable verification command. No forge or origin is required.

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

Integration requires the clean expected base and exact reviewed candidate and
permits only fast-forward. Review findings are advisory. Local orchestration
can still use a cloud model; offline inference needs separate configuration.

## Optional collaboration

```sh
machinist start --from-issue https://gitlab.com/team/project/-/issues/42
machinist publish T1 --provider gitlab
# Or: machinist publish T1 --provider github
```

Authenticate `glab` or `gh` for the selected host. Publication binds to origin
and can retry without repeating local Phases. GitLab supports nested projects
and self-managed hosts; it does not supply native Spec CI or remote Approval.

## Recovery and amendments

```sh
machinist retry --task T1 --phase execute
machinist retry --task T1 --phase execute --fresh
machinist amend --task T1 --feedback "Also name the rejected timezone value."
```

Amendment generates a new Spec requiring fresh Approval. After integration
starts, use a new Task. `machinist continue T1` reports or advances the next
eligible action; it does not bypass Approval or explicit retry.

## Existing GitHub automation

The existing issue/watcher workflow remains available through
`machinist onboard`. Review, commit, push, and merge setup before running
`machinist doctor --run-gates`. For `github.spec_source: github-actions`, add
the selected Spec adapter's declared secret. Execution remains local:

```sh
machinist approve --issue <issue>
machinist run <issue>
machinist review <issue>
```

See the [local workflow](local-workflow.md), [Getting Started](getting-started.md),
[operator runbook](operator-runbook.md), and [trust model](trust-model.md).

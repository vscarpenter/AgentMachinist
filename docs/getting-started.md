# Getting Started with AgentMachinist

AgentMachinist turns a GitHub issue into a draft specification PR, waits for
approval of that exact spec commit, then asks your coding harness to implement
it. The resulting PR is still yours to review and merge.

## What is AgentMachinist?

It is an issue-to-reviewed-PR build pipeline with two human gates:

```text
issue → SPEC → draft PR → APPROVE exact SHA → EXECUTE → tests → ready PR
          machine                 human             machine       human
```

AgentMachinist owns Git operations and GitHub transitions. Each task runs in a
separate worktree or clone, so your active checkout is not used as the harness
workspace. The harness never receives merge authority from AgentMachinist.

## Before you begin

Install and verify:

| Tool | Check |
| --- | --- |
| Git | `git --version` |
| GitHub CLI, authenticated | `gh auth status` |
| uv | `uv --version` |
| One harness | for example, `claude --version` |

You need push and pull-request access to the target GitHub repository.

## Install

```sh
uv tool install agentmachinist
machinist --version
```

Upgrade later with `uv tool upgrade agentmachinist`.

## Set up your repository

From the repository root:

```sh
machinist init
```

This creates `machinist.yaml`, `.machinist/specs/`, the managed approval
workflow, and the configured labels. It does not overwrite an existing config
unless you pass `--force`. `--no-workflows` skips workflow projection.

Before committing the generated files, set a real test gate:

```yaml
tests:
  command: uv run pytest
```

Then verify the installation without changing it:

```sh
machinist doctor
machinist sync-workflows --check
```

Resolve every `FAIL`. Treat a null test command warning as an explicit decision,
not a harmless default.

## Your first agent task

Create a focused issue with acceptance criteria and apply the `agent-task`
label. With the default local dispatcher, start one pass:

```sh
machinist watch --once
```

Or address a specific issue directly:

```sh
machinist spec 7
```

The controller reads issue 7, provisions an isolated workspace, runs the
harness in its spec mode, rejects repository mutations, writes
`.machinist/specs/issue-7-spec.md`, commits and pushes `agent/issue-7`, and
opens a draft PR.

## Review and approve

Read the spec in the draft PR. Approval records both the configured label and
the exact 40-character PR head SHA. Choose one method:

```sh
machinist approve 8
```

Or post the exact PR comment:

```text
/machinist-execute
```

Leading and trailing whitespace are accepted; extra words are not. The managed
workflow honors comments only from owners, members, or collaborators. Applying
the configured `machinist:approved` label manually also causes the workflow to
record the current head SHA.

If anyone changes the spec branch afterward, `machinist status` reports
`approval stale` and execution refuses. Approve the new head again.

Do not mark the draft ready yourself. AgentMachinist uses that transition to
signal that implementation and the configured test gate completed.

`machinist run <issue> --force` is an intentional rework path for a ready PR.
It does not bypass immutable approval: approve that PR's current head again
before forcing a second implementation attempt.

## Spec generation: local or CI

`github.spec_source` assigns Phase 1 to exactly one dispatcher:

```yaml
github:
  spec_source: local
```

- `local` means `machinist watch` generates specs using your local harness
  login. The managed spec workflow is absent.
- `github-actions` means `watch` leaves labeled issues alone and
  `machinist-spec.yml` owns spec generation. Run `machinist sync-workflows`
  after changing the value.

The generated CI path is currently Claude-Code-specific and requires an
`ANTHROPIC_API_KEY` repository secret. It installs the pinned AgentMachinist
release instead of repository HEAD. For other harnesses, use local mode unless
you maintain an appropriate installation step.

## Configuration reference

```yaml
version: 1

harness:
  name: claude-code
  command: null
  timeout_minutes: 30
  spec_timeout_minutes: 10

github:
  repo: null
  spec_source: local
  labels:
    trigger: agent-task
    approved: "machinist:approved"
  poll_interval_seconds: 60

workspace:
  root: ~/.machinist/workspaces
  strategy: worktree
  cleanup: on_success
  branch_prefix: agent/

tests:
  command: null
```

Unknown keys fail validation. `repo: null` lets `gh` infer the repository from
the checkout. Workspace cleanup can be `always`, `on_success`, or `never`;
keeping failed workspaces is useful for diagnosis. `tests.command: null` skips
the gate and is surfaced as a doctor warning.

After changing labels, dispatcher ownership, or the installed package version,
regenerate workflows:

```sh
machinist sync-workflows
git diff -- .github/workflows
```

## Choosing a harness

Set `harness.name` to `claude-code`, `opencode`, `pi`, or `codex`. Local runs
reuse the provider authentication already available to that executable.

```yaml
harness:
  name: codex
  command: /opt/homebrew/bin/codex
```

Support is not identical. Some spec modes have a CLI-enforced read-only tool or
sandbox boundary; OpenCode's plan agent is advisory. All implementations are
checked afterward for harness-created commits, remote branch changes, and
`.machinist/` changes. See the [harness matrix](harnesses.md) and
[trust model](trust-model.md) before unattended operation.

## Troubleshooting

Start with:

```sh
machinist doctor
machinist status
```

Common states and responses:

| State or error | Response |
| --- | --- |
| `awaiting spec` | Run local `watch`/`spec`, or verify the CI dispatcher. |
| `awaiting approval` | Review the draft spec. |
| `approval pending` | The label exists but SHA evidence has not been recorded; approve again. |
| `approval stale` | The branch changed after approval; approve the current head. |
| `approved` | Run `machinist run <issue>` or leave `watch` running. |
| `in review` | Implementation finished; review the PR. |
| Previous run failed | Fix the cause, then run `machinist retry <issue>` and restart a long-running watcher. |
| Workspace already exists | Inspect it first, then remove the worktree/clone intentionally. |
| Managed workflow drift | Run `machinist sync-workflows`, inspect, commit, and push. |

Task Run failures and recovery evidence live under `.machinist/runs/` and are
ignored by Git. A failed implementation is not silently retried. If a crash
happened after the implementation push, the retry reruns the test gate and
marks the existing commit ready without rerunning the harness.

## Operational limits

- Local file locks prevent duplicate work on one machine/process family; they
  are not a distributed lock across multiple hosts. Run one local watcher per
  repository.
- Harness processes run as your OS user. AgentMachinist reduces controller
  credentials and checks postconditions, but it is not a container or VM.
- Provider authentication, quotas, model behavior, and spec quality remain
  external dependencies.
- AgentMachinist produces a ready PR. It does not merge, deploy, or verify a
  deployed runtime.

For restarts, retries, logs, and cleanup, continue with the
[operator runbook](operator-runbook.md).

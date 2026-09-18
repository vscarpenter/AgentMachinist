# Start here: your first AgentMachinist Task

AgentMachinist turns a small coding Task into a reviewed local change. You approve
an exact Spec before implementation, then inspect the result before integration.
The controller owns Git; your coding Harness writes the Spec, edits, and reviews.

```text
Task → Spec → approve exact SHA → Execute → verify → Review → you integrate
```

AgentMachinist never merges automatically or remotely; local integration is explicit.
For the picture, read [How it works](how-it-works.html). For illustrated steps,
use the [visual first-run guide](first-run-guide.html).

## One-time setup

This path works with published **AgentMachinist 0.17.1**. You need Python 3.12+,
`uv`, Git, and an installed, authenticated Harness such as Claude Code or Codex.
See [Harness setup](harnesses.md#authentication) if yours is not ready.

```sh
uv tool install agentmachinist
machinist --version
cd your-repository
```

Use `uv tool upgrade agentmachinist` for an existing installation. Start on a
clean named branch with an initial commit and configured Git author. No forge,
origin, watcher, or root configuration file is required.

## Start one small Task

Use a real test command for your project. Verification runs in an isolated
checkout, so the command must prepare missing dependencies: for example,
`uv run pytest` for a uv project or `npm ci && npm test` with a committed lockfile.

```sh
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
```

The controller saves a Task ID such as `T1`, checks the baseline, writes a Spec,
and stops. Read the Spec, then copy the full Approval command it prints:

```sh
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
```

Approval continues implementation, Verification, and independent Review. Your
current branch is unchanged until you choose to integrate. Review findings are
advisory; a completed report does not mean every finding has been resolved.

## Inspect and integrate

```sh
machinist status T1
```

Read the report at the printed path and inspect the candidate diff. Use the
Spec and candidate SHAs shown by status with `git diff <spec-sha> <candidate-sha>`.
When you accept the change:

```sh
machinist integrate T1
```

Integration fast-forwards only the clean expected base to the exact reviewed
candidate. You are done; [publishing a PR or MR](local-workflow.md#publish-when-useful)
is optional. Local orchestration can still use a cloud model; see the
[trust model](trust-model.md) for the execution boundary.

## If something stops

Use `machinist status T1` and follow its next action. A failed Phase needs an
explicit retry; inspect its Evidence first. [Recovery instructions](local-workflow.md#amend-or-recover)
cover retries, fresh Workshops, and amendments requiring a new Approval.

Once integration starts, amendments require a new Task. Rerun
`machinist integrate T1` to reconcile an interrupted integration.

First start saves settings in `.machinist/runs/local/config.yaml`. Later edits to
root `machinist.yaml` do not update that saved file. See [local settings](operator-runbook.md#local-settings-and-evidence)
for changes and baseline failures. Bounded repair and combined reporting are
unreleased source-checkout additions documented in the [runbook](operator-runbook.md).

## Go further when needed

- [GitHub automation](getting-started.md#github-setup-and-automation): issue intake, hosted Spec generation, and watcher setup.
- [Local workflow reference](local-workflow.md): context files, settings, amendments, and GitLab/GitHub publication.
- [All documentation](README.md) / [web directory](index.html#documentation): configuration, operation, architecture, and historical records.

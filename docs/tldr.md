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

A Task has an ID such as `T1`. Its Spec is the written contract for the change,
named by its exact commit, and Approval authorizes that one commit. The Harness
is your coding-agent CLI, a Workshop is the isolated checkout where it works, and
the candidate is the reviewed commit you integrate. The
[glossary](https://github.com/vscarpenter/AgentMachinist/blob/main/CONTEXT.md) defines the rest.

## One-time setup

This path works with published **AgentMachinist 0.18.0**. You need Python 3.12+,
`uv`, Git, and an installed, authenticated Harness such as Claude Code or Codex.
See [Harness setup](harnesses.md#authentication) if yours is not ready.

```sh
uv tool install agentmachinist
cd your-repository
git config user.name "Your Name"
git config user.email "you@example.com"
machinist rehearse         # optional, free: the whole loop with a fake Harness
machinist doctor --local   # optional, free: checks Git, your Harness login, and the test command
```

The controller ignores your global Git identity, so set the author inside the
repository as shown; without one, its commits use the AgentMachinist identity.
Use `uv tool upgrade agentmachinist` for an existing installation. Start on a
clean named branch with an initial commit. No forge, origin, watcher, or root
configuration file is required.

## Start one small Task

Use a real test command for your project. Verification runs in an isolated
checkout of committed files, so the command must prepare its own dependencies
and must not leave new files that Git does not ignore: `uv run pytest` with a
committed `uv.lock`, or `npm ci && npm test` with a committed lockfile. To try
this on something disposable first, copy the
[example project](https://github.com/vscarpenter/AgentMachinist/blob/main/examples/first-task/README.md).

From here on, `start` and `approve` call your Harness's model, which uses your
provider quota. Each runs in the foreground and can take several minutes.

```sh
machinist start "Reject unknown timezone names in parse_timezone with a ValueError" --test-cmd "uv run pytest"
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
# Read the report, then compare: git diff <spec-sha> <candidate-sha>
machinist integrate T1
```

Status prints the report path and the Spec and candidate SHAs. Integrate only
when you accept the change: it fast-forwards the clean expected base to the
exact reviewed candidate. You are done; [publishing a PR or MR](local-workflow.md#publish-when-useful)
is optional. Local orchestration can still use a cloud model; see the
[trust model](trust-model.md) for the execution boundary.

## If something stops

Use `machinist status T1`; it prints the error, the log directory, and the next
action. A failed baseline needs a fixed verification command and a retry, or a
committed baseline change and a new Task. [Recovery instructions](local-workflow.md#amend-or-recover)
cover retries, fresh Workshops, and amendments requiring a new Approval. Once
integration starts, amendments require a new Task; rerun `machinist integrate T1`
to reconcile an interrupted integration.

First start saves settings in `.machinist/runs/local/config.yaml`. Later edits to
root `machinist.yaml` do not update that saved file. See [local settings](operator-runbook.md#local-settings-and-evidence)
for changes, baseline failures, bounded repair, and reporting.

## Go further when needed

- [GitHub automation](getting-started.md#github-setup-and-automation): issue intake, hosted Spec generation, and watcher setup.
- [Local workflow reference](local-workflow.md): context files, settings, amendments, and GitLab/GitHub publication.
- [All documentation](README.md) / [web directory](index.html#documentation): configuration, operation, architecture, and historical records.

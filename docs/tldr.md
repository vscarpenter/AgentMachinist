# Start here: try AgentMachinist, then make one small change

AgentMachinist helps your coding agent make a change you can check before
accepting it. Read its plan, authorize the work, then inspect the change,
test results, and a separate review before adding it to your project.

```text
Task → Spec → approve exact SHA → Execute → verify → Review → you integrate
```

A Task is one objective, with an ID such as `T1`. The plan is its Spec;
Approval authorizes that exact saved version. Your coding-agent CLI is the
Harness. It works in a separate checkout called a Workshop; the finished
commit is the candidate. See the [glossary](https://github.com/vscarpenter/AgentMachinist/blob/main/CONTEXT.md)
and [workflow diagram](how-it-works.html) when you need them.

## Try the workflow free

This guide covers **AgentMachinist 0.20.0**. You need Python 3.12+, `uv`, and
Git. Install the package, then try the workflow:

```sh
uv tool install agentmachinist
machinist rehearse --guided
```

The rehearsal uses a fake Harness and a disposable project, with no model or API
calls. Read the sample Spec and approve it when prompted. Then inspect the
candidate diff, checks, and Review before accepting it. You can decline either
decision; the printed project path is retained for inspection. No Harness login
or forge setup is needed. The [visual guide](first-run-guide.html) illustrates it.

## Check your own project

Install and authenticate a supported [Harness](harnesses.md#authentication).
Start on a clean named Git branch with an initial commit. Set your author
inside the repository, since the controller ignores global Git identity:

```sh
cd your-repository
git config user.name "Your Name"
git config user.email "you@example.com"
machinist doctor --local
machinist doctor --local --fresh-workshop
```

The first check probes readiness without model work or project tests. The second
explicitly runs your configured tests in a disposable committed checkout; it
may download dependencies. It creates no Task and makes no model call.
Verification must prepare its own dependencies and leave no new unignored files:
use `uv run pytest` with a committed `uv.lock`, or `npm ci && npm test`.
For a disposable real Task, use the [example project](https://github.com/vscarpenter/AgentMachinist/blob/main/examples/first-task/README.md).

## Start, read, and approve

From here, `start`, `revise`, and `approve` call your Harness's model and use
provider quota. They run in the foreground and can take several minutes.

```sh
machinist start "Reject unknown timezone names in parse_timezone with a ValueError" --test-cmd "uv run pytest"
```

Read the printed Spec. If it needs a correction, keep the same Task:

```sh
machinist revise T1 --feedback "Keep the public API unchanged."
```

Read the new Spec, then copy its full Approval command:

```sh
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
```

Approval runs implementation, Verification, and independent Review. Your base
branch stays unchanged. Review findings are advisory; read them before accepting.

## Inspect and accept

```sh
machinist inspect T1
machinist integrate T1
```

Inspect brings the plan, diff, checks, Review, and next action together. Integrate
only when satisfied; it fast-forwards the clean expected base to that candidate.
AgentMachinist never merges automatically or remotely; local integration is explicit.
[Publishing a PR or MR](local-workflow.md#publish-when-useful) is optional.
Harnesses run as your OS user and may use cloud models; see the [trust model](trust-model.md).

## If something stops

Use `machinist inspect T1` or `machinist status T1` for the error and recovery.
A failed baseline needs a fixed test command and retry, or a committed baseline
fix and a new Task. Use `machinist config show --local` and
`machinist config set tests.command "uv run pytest" --local` for saved local settings.
Root settings do not update that copy. [Recovery](local-workflow.md#amend-or-recover)
covers retries and feedback on completed candidates. [GitHub automation](getting-started.md#github-setup-and-automation)
is optional; the [web directory](index.html#documentation) links detailed references.

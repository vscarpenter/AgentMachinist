# Local workflow and optional publication

The local workflow takes a Task through Spec, human Approval, implementation,
verification, independent Review, and explicit local integration. GitHub or
GitLab can supply the initial issue and receive the completed change whenever
you choose to publish it.

This workflow is currently unreleased. From an AgentMachinist source checkout,
install it with `uv tool install --editable .`; then run the commands below in
the repository you want to change. The published 0.13.0 package retains the
[GitHub issue workflow](getting-started.md#github-setup-and-automation).

## Complete one Task

Start in a Git repository with an initial commit, a configured Git author, an
installed Harness, and a verification command appropriate to the project:

```sh
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
```

First start discovers an installed Harness that supports the three Phases and
detects a verification command when the project manifest provides one. Use
`--harness codex` or another installed adapter to select it explicitly. Missing
prerequisites produce a next action before model work begins. The local journey
requires a real verification command; supply `--test-cmd` when detection cannot
find one. Local Review always runs, even if an existing GitHub configuration
disabled its optional Review Phase.

Local settings are stored in `.machinist/runs/local/config.yaml`. Runtime files
are excluded through Git's local exclude file. A pre-existing `machinist.yaml`
continues to provide repository settings; first start does not overwrite it or
generate GitHub workflows, labels, or issue forms.

The verification command must work in an isolated checkout of committed files.
An existing `node_modules/`, `.venv/`, or other ignored dependency directory in
your working repository is not copied into the Workshop. Use a command that
prepares its environment, such as `npm ci && npm test` with a committed lockfile
or `uv run pytest`. A prepared absolute interpreter is another option, provided
its dependencies are installed and tests import the Workshop's code.

Before invoking the Spec Harness, the controller runs baseline verification in
that isolated Workshop. A failing baseline stops before model work. If the
failure is a missing dependency or unsuitable command, edit the required Gate
in `.machinist/runs/local/config.yaml` (`tests.command` or the corresponding
`verification.gates` entry), prepare any external dependencies, then retry the
saved Task:

```sh
machinist retry --task T1 --phase spec
```

Installing dependencies only in your original checkout does not repair the
Workshop environment. If you need to change the committed project baseline,
commit that change and start a new Task from the updated base.

The command saves a Task such as `T1`, verifies the baseline, generates its Spec
in the isolated Workshop, retains the exact Spec commit, and stops. Read the
Spec, then copy the full command printed by the CLI:

```sh
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
```

Approval records the repository, Task, exact Spec SHA, actor, and time. It
continues Execute, the configured Verification Gates, and independent Review
in the foreground. The controller retains a durable local candidate branch and
a report. Review findings are advisory; a completed Review does not mean that
every finding is resolved or that the change is safe to merge.

```sh
machinist status T1
machinist status T1 --json
```

Status shows the current result and one next action. Inspect the report and
diff before accepting the change. To integrate it locally:

```sh
machinist integrate T1
```

Integration requires a clean checkout of the expected base branch, the exact
base and reviewed candidate commits, and fast-forward ancestry. A changed base,
changed candidate, or dirty checkout stops integration. Intent is recorded
before the update so a retry can reconcile an interrupted integration without
silently discarding edits. The command does not push or merge a remote PR/MR.

## Provide existing context

A Markdown file or stdin can provide a richer Task body:

```sh
machinist start "Handle an invalid timezone without crashing" --body-file task.md
cat task.md | machinist start "Handle an invalid timezone without crashing" --body-file -
```

For issue intake, authenticate the appropriate forge CLI and use the exact
issue URL:

```sh
machinist start --from-issue https://github.com/team/project/issues/42
machinist start --from-issue https://gitlab.com/team/subgroup/project/-/issues/42
machinist start --from-issue https://gitlab.example.com/team/project/-/issues/42 --provider gitlab --host gitlab.example.com
```

GitHub uses `gh`; GitLab uses `glab`, authenticated for the selected host. Local
Task IDs remain independent of issue numbers: imported issue 42 can become
`T1`. The external source is saved as provenance. Importing an issue does not
install automation or turn remote labels/reviews into local Approval.

For the existing GitHub issue-template path, `machinist task new --title
"Handle an invalid timezone without crashing" --body-file task.md` accepts
file input; `--body-file -` reads stdin. Lint accepts `##` and GitHub's `###`
field headings. An Objective needs at least six words, and acceptance
checkboxes need observable, non-placeholder text. Drafts are preserved when
lint or publication fails so corrections do not require retyping the Task.

## Amend or recover

An amendment starts with explicit feedback and produces a new Spec:

```sh
machinist amend --task T1 --feedback "Also show which timezone value was rejected."
# Inspect the new Spec and approve its newly printed SHA.
```

The earlier Approval cannot authorize the new Spec. Prior candidate and Task
Run history remain available. A later successful Execute can receive a fresh
Review; repeating Review of the same successful candidate remains blocked.
Once local integration has begun, start a new Task from the current base
instead of amending the integrated Task.

For interrupted or failed work, inspect status and explicitly select the
failed Phase:

```sh
machinist retry --task T1 --phase execute
# Or start a fresh Workshop rather than reuse retained edits:
machinist retry --task T1 --phase execute --fresh
machinist retry --task T1 --phase review
```

Retry validates retained Workshop custody. Recovery after the implementation
commit uses the saved result instead of repeating successful implementation or
verification. `machinist continue T1` advances eligible work or reports the next
human action; it does not grant Approval or replace explicit retry.

```sh
machinist cancel --task T1 --reason "Requirements changed"
machinist cancel --task T1 --clear
```

Cancellation is cooperative and leaves durable Evidence. Resolve the cause and
use the recovery action shown by status before continuing.

## Publish when useful

The completed candidate can remain local, be integrated locally, or be shared
through a forge. Configure a single origin matching the intended repository
and authenticate `gh` or `glab` for that host:

```sh
machinist publish T1 --provider github
machinist publish T1 --provider gitlab
machinist publish T1 --provider gitlab --host gitlab.example.com
```

Each command is an explicit publication decision. The controller checks the
approved Spec, successful Execute and Review Evidence, exact candidate branch,
and forge/origin identity. It records the intended SHA and remote lease before
pushing, then verifies the resulting PR/MR's repository, number, branch, base,
head, and open state. An existing unowned branch or closed/merged change cannot
be silently reused.

If publishing fails, run the same publish command again. A push or PR/MR
creation whose response was lost can be reconciled without rerunning the
Harness or Verification Gates. An amended candidate may update the same change
only against the last published SHA owned by that Task. Resolve a pending
publication before replacing its candidate.

GitLab support covers issue intake and MR publication through `glab`, including
nested project paths and explicitly selected self-managed hosts. Native GitLab
CI Spec dispatch and remote GitLab Approval are not part of this workflow.
Existing GitHub Actions Approval and watcher commands continue separately.

## Use it alone or with a small team

For a solo developer, start with a bounded bug fix or small enhancement and an
observable acceptance test. The useful handoff is a Spec to approve followed by
a diff, verification Evidence, and a Review report to inspect. Direct Harness
use can remain simpler for a tiny edit.

For a small team, use one persistent runner checkout per repository. Team
members can write issues and discuss PRs/MRs on their existing forge; the runner
operator imports Tasks, approves Specs, and publishes reviewed results. Local
Task records and Claims belong to that checkout. Multiple laptops are not
coordinated workers, and local daily budgets are not shared team quotas.

Local orchestration means that no forge or server is required for the Task
lifecycle. A Harness may still send code to a cloud model. Offline inference
requires a local provider, downloaded models, cached dependencies, and separate
network-denied validation. AgentMachinist's custody checks reduce credentials
and detect violations; they do not isolate a hostile process running as your
OS user. See the [trust model](trust-model.md).

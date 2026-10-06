# Local workflow and optional publication

For a first Task, follow [Start here](tldr.md). This reference covers local
settings, context, recovery, and optional publication in detail.
See the [documentation index](README.md) for other topics.

The local workflow takes a Task through Spec, human Approval, implementation,
verification, independent Review, and explicit local integration. GitHub or
GitLab can supply the initial issue and receive the completed change whenever
you choose to publish it.

This guide covers AgentMachinist 0.20.0: guided rehearsal, initial Spec revision,
local inspection/settings selectors, and readiness in a fresh Workshop.
Use the [package installation](getting-started.md#install) and run local commands
in the repository you want to change. The existing
[GitHub issue workflow](getting-started.md#github-setup-and-automation)
remains available.

## Complete one Task

Try `machinist rehearse --guided` first. It uses a fake Harness in a disposable
project, pauses to let you read and approve the plan, then shows the change,
checks, and Review before you accept it. It makes no model or API calls;
`--harness` is the explicit option that invokes configured, potentially paid
Harnesses. Declining either decision retains the printed project path.

Start on a named branch in a clean Git repository with an initial commit, a
repository-local Git author, an installed and authenticated Harness, and a
verification command appropriate to the project. This checked-out branch and
commit become the Task's integration base:

```sh
machinist start "Reject unknown timezone names in parse_timezone with a ValueError" --test-cmd "uv run pytest"
```

First start reuses configured Harness profiles and required Verification Gates
when present. Otherwise it discovers an installed Harness that supports the
three Phases and detects a verification command from the project manifest. Use
`--harness codex` or another installed adapter to select it explicitly. Missing
Harness executables or a required Gate produce configuration guidance before
model work. Setup does not probe provider login or model access; authenticate
the Harness and set a repository-local Git author yourself before starting. Supply
`--test-cmd` when detection cannot find a required verification command. Local Review always runs, even if an existing GitHub configuration
disabled its optional Review Phase.

Local settings are stored in `.machinist/runs/local/config.yaml`. First start
copies applicable settings from a pre-existing `machinist.yaml`, then sets
local Spec dispatch, disables managed workflows and telemetry export, and
enables Review. Subsequent local commands read the saved local configuration;
changes to the root file do not automatically propagate. Runtime files are
excluded through Git's local exclude file. Setup preserves the root config and
does not generate GitHub workflows, labels, or issue forms.

To inspect or change saved local settings, use the explicit `--local` selector:

```sh
machinist config show --local
machinist config set tests.command "uv run pytest" --local
machinist config validate --path .machinist/runs/local/config.yaml
```

`--path .machinist/runs/local/config.yaml` remains an alternative. Without a
root `machinist.yaml`, these commands default to the saved local file.

The `tests.command` example applies when you use the single Gate. If you have
named `verification.gates`, edit those entries instead; the two forms cannot
be combined. Local commands require at least one required Gate,
`review.enabled: true`, `github.spec_source: local`,
`github.manage_workflows: false`, `telemetry.otlp_endpoint: null`, and an
absolute Workshop root outside the repository. Generic config validation
checks the shared schema; local commands also check these local constraints.
Until the first Task is recorded, `start --harness` and `--test-cmd` replace the
saved settings. Afterwards they must agree with the saved values; change the
local file to change them. Changes to root `machinist.yaml`, including
this repository's workflow, format, lint, type, and coverage Gates, do not update
an already saved local configuration.

The verification command must work in an isolated checkout of committed files.
An existing `node_modules/`, `.venv/`, or other ignored dependency directory in
your working repository is not copied into the Workshop. Use a command that
prepares its environment, such as `npm ci && npm test` with a committed lockfile
or `uv run pytest` with a committed `uv.lock`. A command that leaves new files in
the Workshop stops the Spec Phase before any Harness work, and the error names
those files. A prepared absolute interpreter is another option, provided
its dependencies are installed and tests import the Workshop's code.

Before invoking the Spec Harness, the controller runs baseline verification in
that isolated Workshop. A failing baseline stops before model work, and
`machinist status T1` reports `baseline failed` with the Gate's error and log
directory. If the
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
Spec. If the initial plan needs correction, use `machinist revise T1 --feedback
"Keep the public API unchanged."` before implementation. Read its new Spec;
earlier Approval cannot authorize it. When satisfied, copy the full command
printed by the CLI:

```sh
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
```

Approval records the repository, Task, exact Spec SHA, actor, and time. It
continues Execute, the configured Verification Gates, and independent Review
in the foreground. The controller retains a durable local candidate branch and
a report. Review findings are advisory; a completed Review does not mean that
every finding is resolved or that the change is safe to merge.

```sh
machinist inspect T1
machinist inspect T1 --json
```

Optional bounded repair is off by default.
Set `verification.repair.max_attempts: 1` in the saved local configuration to
permit one additional Harness invocation after an ordinary required Gate failure
inside the active Execute run. It defaults to `0`. The extra invocation and all
final Gates share `verification.repair.timeout_minutes` (default 10, range 1–240).
The Harness and individual Gate timeouts still apply. Detected control errors
and abnormal Gate outcomes disqualify repair; an ordinary nonzero exit cannot
establish whether its cause is code or infrastructure. See the
[repair contract](getting-started.md#bounded-verification-repair). Failed runs
still require explicit retry. Spec baseline verification and readiness checks
do not invoke repair.

Status shows the Spec text, exact commits, report path, and one next action.
`status T1 --json` also includes saved publication and integration results.
Without an ID, `machinist status` lists local Tasks once this checkout has local
configuration; `machinist status T1 --watch --interval 2` shows changed
snapshots. Local status does not query the forge.

The candidate is retained on `<branch_prefix>task-1` (`agent/task-1` by
default). `machinist inspect T1` brings together the plan, candidate diff,
Verification, Review, and recovery guidance without contacting a forge.
To compare commits directly:

```sh
git diff <full-spec-commit-sha> <full-candidate-commit-sha>
```

Replace both placeholders with the displayed commits. This is the same diff
used by independent Review. For an amended Task, also compare the original base
branch with the candidate to inspect the accumulated change. To integrate the
accepted candidate locally:

```sh
machinist integrate T1
```

Integration requires a clean checkout of the expected base branch, the exact
base and reviewed candidate commits, and fast-forward ancestry. A changed base,
changed candidate, or dirty checkout stops integration. Intent is recorded
before the update so a retry can reconcile an interrupted integration without
silently discarding edits. The command does not push or merge a remote PR/MR.

Local status and completion receipts name
the next human activity and supply commands with the saved Task ID and exact
Spec SHA where needed. The short path is `start`, read and approve the Spec, inspect the
candidate and Review report through `inspect T1`, then `integrate T1`.
After integration, the CLI reports that local work is complete and offers
optional publication commands with `--provider github` or `--provider gitlab`.
Choose one only when you want to share the candidate. Text guidance is kept
out of JSON output; the existing structured fields remain available.

## Optional local readiness

`machinist doctor --local` is an optional readiness check.
You can still begin with `start` directly; this check adds no required
onboarding step.

```sh
machinist doctor --local
machinist doctor --local --json
```

It uses the same configuration resolution as `start`: saved local settings
win, otherwise it previews applicable root settings and discovery without
saving them. It checks a committed named Git branch, Git identity, clean
checkout, Workshop location, Harness executables and Phase support, and
required Verification Gates and their command entry points. Adapter-supported
version, compatibility, and authentication probes run without a model call;
plugins without authentication probes need manual verification. Authentication
readiness does not establish quotas or access to a particular model.

The identity check reads only repository-local `user.name` and `user.email`;
without them it warns that commits use the AgentMachinist identity. An
unsafe runtime-exclusion path fails readiness. If exclusion is not established,
the check warns that `start` must still apply and verify it against your ignore
rules; it does not modify those rules to test them.

By default, the controller creates no Task, Claim, Workshop, runtime/config
file, Git exclusion, or ref. It makes no forge or release-update request and
does not run Verification Gates. A configured command being available does
not mean its tests passed.

To explicitly execute the configured Gates:

```sh
machinist doctor --local --fresh-workshop
# Or run in your current checkout:
machinist doctor --local --run-gates
```

`--fresh-workshop` uses a disposable clone of committed `HEAD` and the shared
Verification engine. It leaves controller Git metadata unchanged and creates
no Task or model call. Project commands can write files or download dependencies.
It checks the committed baseline that a new Task would receive; ignored local
dependency directories are not copied.

`--run-gates` instead uses your **controller checkout**. It does not prove
the isolated Workshop baseline, which `start` still checks before Spec work.
Resolve reported readiness failures before running Gates. Plain `doctor` runs
these same checks when no root `machinist.yaml` exists and checks the existing
GitHub setup otherwise.

## Provide existing context

A Markdown file or stdin can provide a richer Task body:

```sh
machinist start "Handle an invalid timezone without crashing" --body-file ../task.md
# Or read stdin instead:
# cat ../task.md | machinist start "Handle an invalid timezone without crashing" --body-file -
```

These examples keep `task.md` outside the repository. The clean-checkout
requirement still applies when reading stdin. If the file is inside the
repository, commit it first or keep it in an ignored location; a new untracked
Task file otherwise makes `start` refuse the checkout.

For issue intake, authenticate the appropriate forge CLI and use the exact
issue URL:

```sh
machinist start --from-issue https://github.com/team/project/issues/42
# Or GitLab:
# machinist start --from-issue https://gitlab.com/team/subgroup/project/-/issues/42
# Or self-managed GitLab:
# machinist start --from-issue https://gitlab.example.com/team/project/-/issues/42 --provider gitlab --host gitlab.example.com
```

GitHub uses `gh`; GitLab uses `glab`. Authenticate for the selected host before
importing, for example `gh auth login --hostname github.com` or
`glab auth login --hostname gitlab.example.com`. Import accepts HTTPS issue URLs
without credentials, query strings, or fragments. `--host`, when supplied,
must match the URL. `--from-issue` cannot be combined with an objective or body
file. Local
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

For an initial saved Spec before a candidate exists, keep its Task and revise
the plan:

```sh
machinist revise T1 --feedback "Keep the public API unchanged."
# Read the new Spec and approve its newly printed SHA.
```

Revision generates a new Spec, invalidates earlier Approval, and stops for a
fresh human decision. It does not implement the change. Recover a failed Phase
with explicit retry first.

Amendment is for feedback on a completed, verified and reviewed candidate:

```sh
machinist amend --task T1 --feedback "Also show which timezone value was rejected."
# Inspect the new Spec and approve its newly printed SHA.
```

The earlier Approval cannot authorize the new Spec. Prior candidate and Task
Run history remain available. A later successful Execute receives a fresh
Review; continuing the same successful candidate does not repeat that Review.
Once local integration has begun, start a new Task from the current base
instead of amending the integrated Task.

For interrupted or failed work, inspect status and explicitly select the
failed Phase:

```sh
machinist retry --task T1 --phase execute
# Or start a fresh Workshop rather than reuse retained edits:
# machinist retry --task T1 --phase execute --fresh
# For a failed Review instead:
# machinist retry --task T1 --phase review
```

Local retry runs immediately in the foreground and requires the current failed
Phase. Review saves the Harness's raw output as `harness-report.txt` in its
attempt's log directory under `.machinist/runs/local/logs/`, and an invalid
report names that file in its error. Read it before retrying Review, because
the same Harness and prompt usually fail the same way. Execute retry resumes retained edits by default; `--fresh` selects a
new Workshop. It clears a cancellation marker and validates retained Workshop
custody before reuse. Recovery after the implementation
commit uses the saved result instead of repeating successful implementation or
verification. `machinist continue T1` advances eligible work or reports the next
human action; it does not grant Approval or replace explicit retry. If a repair
was interrupted or ended in failure, timeout, or cancellation, use
`machinist retry --task T1 --phase execute --fresh`: resume cannot replay paid
repair work.
A completed repair with valid retained state can resume Verification only within
its saved deadline. Fresh Execute attempts receive a new repair budget; ordinary
resume does not replenish it.

If a Harness keeps failing for reasons outside the Task, such as a rate limit or
an outage, choose another installed Harness or model for one retry:

```sh
machinist retry --task T1 --phase execute --fresh --harness codex
machinist retry --task T1 --phase review --harness claude-code --model claude-opus-5-5
```

The choice applies to every Phase that retry runs, including the Review that
follows Execute, and it is never saved. Task Run Evidence records the Harness
and model each Phase used. Approval still covers the same Spec commit. Execute
requires `--fresh` with a new choice, because resuming would mix retained edits
from two Harnesses. To change the Harness for later Tasks, edit the saved
settings with `machinist config set`. The controller never switches Harnesses
on its own.
Changing configured Gates, limits, instruction overlays, or enabled repair
settings requires a fresh Execute attempt. If retained implementation work
already consumed its instructions, passing verification does not reread them.
A new repair invocation on resume must reconstruct those instructions and
match their saved digest; missing or changed files require `--fresh`.

```sh
machinist cancel --task T1 --reason "Requirements changed"
```

Cancellation is cooperative and leaves durable Evidence. Resolve the cause
before clearing it with `machinist cancel --task T1 --clear`, then use the
recovery action shown by status before continuing.

## Publish when useful

The completed candidate can remain local, be integrated locally, or be shared
through a forge. Configure exactly one origin URL matching the intended
repository and authenticate `gh` or `glab` for that host:

```sh
machinist publish T1 --provider github
# Or GitLab:
# machinist publish T1 --provider gitlab
# Or self-managed GitLab:
# machinist publish T1 --provider gitlab --host gitlab.example.com
```

The origin repository must have the Task's base branch. Use an HTTPS or SSH
Git URL; separate `remote.origin.pushurl` settings, Git URL rewrites, and
ambiguous origin URLs are rejected. For HTTPS GitLab publication the controller
uses host-bound `glab` credentials for its Git subprocesses. SSH transport needs
your configured SSH authentication as well as forge API authentication.
`--host` confirms the origin host; it does not redirect publication to a
different repository. Issue provenance does not select the publication target.

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

## Command and storage boundaries

Local Tasks and legacy issue numbers have separate records and recovery paths:

| Operation | Local Task workflow | Existing GitHub issue workflow |
| --- | --- | --- |
| Create | `start` with text or explicit issue import | `task new`; local Spec source: `spec` directly or trigger label plus `watch`; hosted Spec source: trigger label starts GitHub Actions |
| Approve | `approve --task T1 --spec-sha <sha>` continues in foreground | `approve --issue 42` or `--pr 8` requests trusted workflow Evidence; wait for `explain 42` to report `approved` before the first Execute |
| Revise initial plan | `revise T1 --feedback "text"` generates a new Spec before a candidate exists | `spec 42 --revise` revises a draft Spec |
| Resume | `continue T1`; failure requires `retry --task T1 --phase execute` | `retry 42 --phase execute --run --resume` explicitly reuses edits |
| Inspect | `inspect T1`, `inspect T1 --json` for plan/diff/checks/Review; `status T1` for state | `explain 42` for live state/next action; `inspect 42`, `runs --issue 42` for Evidence |
| Aggregate | `report --source local` | `report --source legacy`; the default `report` combines both namespaces |
| Configure | `config show --local` and `config set <key> <value> --local`; explicit `--path` remains available | `config show` reads `machinist.yaml` when present |
| Schedule | Foreground commands | `watch`, `queue`, and macOS `service` |
| Deliver | Explicit `integrate T1` and/or `publish T1 --provider gitlab` (or `github`) | Ready GitHub PR; human remote merge |

Local records, Phase history, configuration, and reports live under
`.machinist/runs/local/`; Task records and reports are in its `tasks/` directory.
The Spec itself is committed at `.machinist/specs/task-1-spec.md`. Preserve
runtime records for recovery; do not commit them or edit Task JSON manually.

Plain `doctor` is the root GitHub setup preflight when `machinist.yaml` exists
and runs local readiness otherwise; `doctor --local`
checks local readiness explicitly as described above. `runs`, numeric `inspect 42`, `explain`, and portfolio `status --all` read the legacy
issue-run namespace under `.machinist/runs/`. Aggregate `report` reads both
namespaces by default; use
`machinist report --source local --since 30d --json`
for foreground Tasks only, without root configuration or forge access.
`status --local` is an offline view of legacy issue runs only when no local
configuration is present. In a mixed checkout, default status selects local
Tasks; use `runs`/`inspect 42` for legacy Evidence. `inspect T1` always reads a local Task.

Report `success_rate` measures terminal Phase attempts. `first_pass_execute`
measures terminal first Execute attempts that succeeded without repair. Phase
attempts enter `--since` windows by their last saved update, not their start.
Local delivery counts are current stored snapshots of Tasks updated in the window, not delivery
events or acceptance rates. Missing token usage is unknown; inspect
`usage_coverage` before interpreting totals. Local/all reports remain local
unless `--otlp-endpoint` is explicitly supplied; those exports omit repository
identity. See the
[reporting reference](getting-started.md#local-evidence-and-repository-portfolio)
for repair metrics, window semantics, and export behavior.

Watcher queue windows, daily Task Run budgets, notifications, and the managed
service do not govern foreground local Tasks. `clean` lists both kinds of Workshop
and works without a root `machinist.yaml`; `clean --task T1` removes one local
Task's retained Workshops. Local success cleanup follows
`workspace.cleanup`; retained failures should remain available for retry.

## Use it alone or with a small team

For a solo developer, start with a bounded bug fix or small enhancement and an
observable acceptance test. The useful handoff is a Spec to approve followed by
a diff, verification Evidence, and a Review report to inspect. Direct Harness
use can remain simpler for a tiny edit.

For a small team, use one persistent runner checkout per repository. Team
members can write issues and discuss PRs/MRs on their existing forge; the runner
operator imports Tasks, approves Specs, and publishes reviewed results. Local
Task records and Claims belong to that checkout. Multiple laptops are not
coordinated workers. Watcher daily budgets do not cap foreground local work
and are not shared team quotas.

Local orchestration means that no forge or server is required for the Task
lifecycle. A Harness may still send code to a cloud model. Offline inference
requires a local provider, downloaded models, cached dependencies, and separate
network-denied validation. AgentMachinist's custody checks reduce credentials
and detect violations; they do not isolate a hostile process running as your
OS user. See the [trust model](trust-model.md).

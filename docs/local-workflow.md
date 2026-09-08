# Local workflow and optional publication

The local workflow takes a Task through Spec, human Approval, implementation,
verification, independent Review, and explicit local integration. GitHub or
GitLab can supply the initial issue and receive the completed change whenever
you choose to publish it.

This workflow is available in AgentMachinist 0.15.0. Install it with
`uv tool install agentmachinist`, or upgrade an existing tool installation with
`uv tool upgrade agentmachinist`; then run the commands below in the repository
you want to change. Optional local readiness is new in 0.15.0. The existing
[GitHub issue workflow](getting-started.md#github-setup-and-automation)
remains available. See [installation](getting-started.md#install) for an optional
editable source setup.

## Complete one Task

Start on a named branch in a clean Git repository with an initial commit, a
configured Git author, an installed and authenticated Harness, and a
verification command appropriate to the project. This checked-out branch and
commit become the Task's integration base:

```sh
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
```

First start reuses configured Harness profiles and required Verification Gates
when present. Otherwise it discovers an installed Harness that supports the
three Phases and detects a verification command from the project manifest. Use
`--harness codex` or another installed adapter to select it explicitly. Missing
Harness executables or a required Gate produce configuration guidance before
model work. Setup does not probe provider login or model access; authenticate
the Harness and configure Git author identity yourself before starting. Supply
`--test-cmd` when detection cannot find a required verification command. Local Review always runs, even if an existing GitHub configuration
disabled its optional Review Phase.

Local settings are stored in `.machinist/runs/local/config.yaml`. First start
copies applicable settings from a pre-existing `machinist.yaml`, then sets
local Spec dispatch, disables managed workflows and telemetry export, and
enables Review. Subsequent local commands read the saved local configuration;
changes to the root file do not automatically propagate. Runtime files are
excluded through Git's local exclude file. Setup preserves the root config and
does not generate GitHub workflows, labels, or issue forms.

To inspect or change these settings, target the local file explicitly:

```sh
machinist config show --path .machinist/runs/local/config.yaml
machinist config set tests.command "uv run pytest" --path .machinist/runs/local/config.yaml
machinist config validate --path .machinist/runs/local/config.yaml
```

The `tests.command` example applies when you use the single Gate. If you have
named `verification.gates`, edit those entries instead; the two forms cannot
be combined. Local commands require at least one required Gate,
`review.enabled: true`, `github.spec_source: local`,
`github.manage_workflows: false`, `telemetry.otlp_endpoint: null`, and an
absolute Workshop root outside the repository. Generic config validation
checks the shared schema; local commands also check these local constraints.
Later `start --harness` or `--test-cmd` flags must agree with the saved settings;
change the local file to change them.

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

Status shows the Spec text, exact commits, report path, and one next action.
`status T1 --json` also includes saved publication and integration results.
Without an ID, `machinist status` lists local Tasks once this checkout has local
configuration; `machinist status T1 --watch --interval 2` shows changed
snapshots. Local status does not query the forge.

The candidate is retained on `<branch_prefix>task-1` (`agent/task-1` by
default). Inspect the report at the printed path and the diff from your recorded
base to the candidate before accepting it. To integrate it locally:

```sh
machinist integrate T1
```

Integration requires a clean checkout of the expected base branch, the exact
base and reviewed candidate commits, and fast-forward ancestry. A changed base,
changed candidate, or dirty checkout stops integration. Intent is recorded
before the update so a retry can reconcile an interrupted integration without
silently discarding edits. The command does not push or merge a remote PR/MR.

## Optional local readiness

**New in 0.15.0:** `machinist doctor --local` adds an optional readiness check.
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

Identity checks respect the controller's existing commit-author fallback. An
unsafe runtime-exclusion path fails readiness. If exclusion is not established,
the check warns that `start` must still apply and verify it against your ignore
rules; it does not modify those rules to test them.

By default, the controller creates no Task, Claim, Workshop, runtime/config
file, Git exclusion, or ref. It makes no forge or release-update request and
does not run Verification Gates. A configured command being available does
not mean its tests passed.

To explicitly execute the configured Gates:

```sh
machinist doctor --local --run-gates
```

This uses the existing Verification engine in your **controller checkout**;
project commands can write files or download dependencies. It does not prove
the isolated Workshop baseline, which `start` still checks before Spec work.
Resolve reported readiness failures before running Gates. Plain `doctor`
continues to check the existing GitHub setup.

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

Amendment requires a completed, verified and reviewed candidate. It cannot
revise an initial Spec awaiting Approval; if you reject that Spec, start a new
Task with corrected intent. Supply feedback on a completed candidate to produce
a new Spec:

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
Phase. Execute retry resumes retained edits by default; `--fresh` selects a
new Workshop. It clears a cancellation marker and validates retained Workshop
custody before reuse. Recovery after the implementation
commit uses the saved result instead of repeating successful implementation or
verification. `machinist continue T1` advances eligible work or reports the next
human action; it does not grant Approval or replace explicit retry.

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
| Create | `start` with text or explicit issue import | `task new`, trigger label, then `spec` or `watch` |
| Approve | `approve --task T1 --spec-sha <sha>` continues in foreground | `approve --issue 42` or `--pr 8` requests trusted workflow Evidence; wait for `explain 42` to report `approved` before the first Execute |
| Resume | `continue T1`; failure requires `retry --task T1 --phase execute` | `retry 42 --phase execute --run --resume` explicitly reuses edits |
| Inspect | `status T1`, `status T1 --json`, printed report | `explain 42` for live state/next action; `inspect 42`, `runs --issue 42`, `report` for Evidence |
| Configure | `config show --path .machinist/runs/local/config.yaml` | `config show` reads `machinist.yaml` by default |
| Schedule | Foreground commands | `watch`, `queue`, and macOS `service` |
| Deliver | Explicit `integrate T1` and/or `publish T1 --provider gitlab` (or `github`) | Ready GitHub PR; human remote merge |

Local records, Phase history, configuration, and reports live under
`.machinist/runs/local/`; Task records and reports are in its `tasks/` directory.
The Spec itself is committed at `.machinist/specs/task-1-spec.md`. Preserve
runtime records for recovery; do not commit them or edit Task JSON manually.

Plain `doctor` remains the root GitHub setup preflight; the 0.15.0 candidate's
`doctor --local` checks local readiness as described above. `runs`, `inspect`, `explain`,
`report`, and portfolio `status --all` read the legacy issue-run namespace under
`.machinist/runs/`; they do not aggregate the nested local Task namespace.
`status --local` is an offline view of legacy issue runs only when no local
configuration is present. In a mixed checkout, default status selects local
Tasks; use `runs`/`inspect` for legacy Evidence.

Watcher queue windows, daily Task Run budgets, notifications, and the managed
service do not govern foreground local Tasks. `clean` manages legacy Workshops;
there is no local `clean --task` command. Local success cleanup follows
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

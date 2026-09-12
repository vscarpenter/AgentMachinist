# Getting Started with AgentMachinist

AgentMachinist takes a Task through an exact human-approved Spec, isolated
implementation, verification, and independent Review. You can integrate the
reviewed candidate locally and optionally publish it to GitHub or GitLab.

## What is AgentMachinist?

It is a development workflow with two human Gates:

```text
Task → Spec → human Approval of exact SHA → Execute → verify → Review
                                                        → human review/integration
```

AgentMachinist owns Git operations and optional forge transitions. Each Task runs in a
separate worktree or clone, so your active checkout is not used as the harness
workspace. The harness never receives merge authority from AgentMachinist.

## Before you begin

Install and verify:

| Tool | Check |
| --- | --- |
| Git | `git --version` |
| uv | `uv --version` |
| One harness | for example, `claude --version` |

The local workflow requires no origin or forge account. Optional GitHub
operations require authenticated `gh` and access to the target repository;
GitLab operations require authenticated `glab` for the selected host.
The core CLI is tested on macOS and Linux with Python 3.12–3.14. The managed
LaunchAgent integration is macOS-only; Linux users can schedule
`machinist watch --once` with their existing service manager.

## Install

AgentMachinist 0.16.0 includes the guided local workflow, optional GitHub/GitLab
intake and publication, existing GitHub issue automation, and next-step CLI
guidance. Local readiness, exact remote-base validation, and bounded diagnostics
introduced in 0.15.0 remain available. Install it, then change into the repository
you want to work on:

```sh
uv tool install agentmachinist
machinist --version
```

Upgrade an existing tool installation with:

```sh
uv tool upgrade agentmachinist
machinist --version
```

Confirm that `machinist --version` reports 0.16.0 or newer for the completion
guidance described here. `machinist doctor --local` is available since 0.15.0.
For contributing to AgentMachinist, an editable installation is optional:
run `uv tool install --editable .` from its source
checkout, then enter the repository you want to change. An editable install
tracks that checkout instead of the published package; use its Git and `uv sync`
workflow to update it.

Managed GitHub workflows pin the installed controller version; this repository's
own development workflows use `github.spec_install: checkout`.

To find out whether an upgrade is waiting, run:

```sh
machinist update-check
```

It reads the latest published release from PyPI, compares it with the copy you
are running, and — when a newer release exists — prints the upgrade command
that matches how this copy was installed (`uv tool upgrade`, `pipx upgrade`,
`pip install --upgrade`, or `git pull && uv sync` for a source checkout). The
command runs without a repository or a `machinist.yaml`, exits non-zero when
the update result is unknown (for example, a network or version-parsing error),
and supports `--json` for scripts.
`machinist doctor` runs the same check and reports an available release as a
warning.

Run inside a configured repository, it adds one more line: whether the managed
GitHub workflows still match this installation. Managed workflows are projected
files, so a workflow change takes effect only after `machinist sync-workflows`.
Upgrading the package alone can leave the previous workflow in place, and that
line is how you find out without running `doctor`. `machinist watch` prints the
same advisory at startup. It never blocks a command and never appears in
`update-check --json`.

Set `MACHINIST_NO_UPDATE_CHECK=1` to disable release-update probes. Package
installation and configured verification commands can still contact package
registries; this setting does not make the workflow offline.

## Set up your repository

For a local foreground Task, start on a named branch in a clean repository
with an initial commit and configured Git author. This branch and commit become
the Task's integration base:

```sh
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
# Read the Spec; use the exact SHA printed by start:
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
# Approval continues Execute, verification, and independent Review.
machinist status T1
# Inspect the candidate diff and report, then:
machinist integrate T1
```

**New in 0.16.0:** Completion output supplies the next activity and a command
using your saved Task ID. Read the Spec before copying its exact Approval
command, and inspect the candidate and Review report before integration.
Successful integration reports completion and presents publication as optional,
with an explicit `--provider github` or `--provider gitlab` command.

First start uses configured Harness profiles and required Verification Gates
when present, otherwise it discovers an installed Harness with support for
Spec, Execute, and Review and a manifest-backed verification command. It writes
`.machinist/runs/local/config.yaml`. It copies applicable root configuration
once, enables Review, disables managed workflows and telemetry export, and
adds runtime exclusion through Git's local exclude file. It preserves the
root `machinist.yaml`; later root changes do not update the local copy.
Authenticate the Harness before starting. A missing required Gate needs an
explicit `--test-cmd` before model work. Independent Review always runs for
this local journey, even when legacy GitHub settings disabled Review.

The command must work from the Workshop's isolated committed checkout. Ignored
dependency folders such as `node_modules/` and `.venv/` are not copied from your
working repository. Commands such as `npm ci && npm test` or `uv run pytest`
can prepare that environment. Baseline verification runs before the Spec
Harness; on dependency or command failure, correct the required Gate in
`.machinist/runs/local/config.yaml` and retry with
`machinist retry --task T1 --phase spec`. See the local workflow guide for
prepared-interpreter and changed-baseline cases.

Integration is explicitly requested and requires the clean expected base and
exact reviewed candidate to permit a fast-forward. It does not push or merge
remotely. Use `machinist publish T1 --provider github` or
`machinist publish T1 --provider gitlab` when you choose to share that candidate.
See the [local workflow guide](local-workflow.md) for issue import, file/stdin
input, amendments, exact-SHA Approval, and publication recovery.

With local configuration present, `machinist status` lists local Tasks and
`machinist status T1 --json` reads one Task without forge requests. `machinist continue T1`
advances eligible Phases but never grants Approval or silently retries a
failure. Local retry runs immediately and resumes Execute by default:

```sh
machinist retry --task T1 --phase execute
machinist retry --task T1 --phase execute --fresh
machinist amend --task T1 --feedback "Also name the rejected timezone value."
```

Local amendment requires a completed, verified and reviewed candidate and
creates a new Spec requiring fresh Approval and Review. Use retry to recover a
failed Phase first. To reject an initial Spec awaiting Approval, start a new
Task with corrected intent; local amendment cannot revise that initial Spec.
Once integration begins, start a new Task from the updated base instead.

Local setup and the existing GitHub setup use separate configuration and run
namespaces. Plain `doctor`, `watch`, `queue`, `runs`, `inspect`, `explain`, `report`,
`clean`, and portfolio `status --all` retain their legacy scope. They do not
manage or aggregate `T1` records. See the [command and storage
boundaries](local-workflow.md#command-and-storage-boundaries) before operating
both workflows in one checkout.

### GitHub setup and automation

The remaining setup instructions configure the existing GitHub issue/watcher
integration. From the repository root:

```sh
machinist onboard
```

This creates `machinist.yaml`, `.machinist/specs/`, the sealed GitHub issue
form, the managed approval workflow, and the configured labels. It also idempotently adds
`/.machinist/runs/` to `.gitignore` so runtime records are not committed. It
preserves an existing valid config. A recognized partial setup can be resumed
by rerunning `machinist onboard`; valid config and operator preferences are
retained. Conflicting setup flags stop with configuration guidance. Use
`machinist config set` to change a saved choice before resuming. Only the
lower-level `init --force` command explicitly overwrites configuration;
`onboard` has no `--force` option.

`onboard` uses the same renderer as `init`. Add `--setup-pr` when setup should
land through review: initial setup requires a clean default branch, creates
`chore/agentmachinist-setup`, commits only its managed allowlist, pushes it,
and opens a draft PR. Local readiness is checked before publishing the setup PR;
full doctor verifies deployed workflows after you merge setup. A recognized
setup branch containing only managed adoption changes can be resumed, and an
existing draft setup PR is reused. Unrelated committed or working-tree changes
are not swept into adoption. If the default branch already contains the exact
setup, no new PR is needed. Before a real Task, prove the controller flow
without GitHub or model cost:

```sh
machinist rehearse
```

The default rehearsal runs the production local Phases with real Git,
verification, Review, and explicit integration. Its fake Harness is deterministic
and uses no model or API; it invokes no external Harness process.
`machinist rehearse --harness` is the explicit opt-in to run configured
profiles in the disposable repository. It selects saved local Harness profiles
when available, otherwise root configuration. Rehearsal uses its own fixture
verification command, not your project's Gates or instruction overlays, so it
does not establish that your project's isolated baseline passes.

In a terminal, GitHub `onboard` walks you through the choices that matter on the first
run, each with a one-line explanation and a safe default — `init` is the same
setup step without the guided receipt:

- **Dispatch mode** — `local` (the `machinist watch` daemon runs the Spec
  Phase for GitHub issues on your machine) or `github-actions` (CI runs it;
  requires the selected
  Spec adapter's declared repository secret).
- **Managed workflows** — install the Machinist-owned
  `.github/workflows/machinist-*.yml` files.
- **Harness** — `claude-code`, `codex`, `opencode`, `pi`, or an installed v1
  adapter plugin. Managed GitHub Actions dispatch renders the selected
  adapter's pinned install and secret metadata.
- **Test gate** — confirm the auto-detected command, or pick your language
  for a suggested one (`pytest`, `npm test`, `cargo test`, `go test ./...`,
  `mvn test`), type your own, or explicitly skip the Gate for the legacy
  workflow. Foreground local Tasks require a required Gate.
- **Notifications** — failures only, all key events, or none.

Flags pre-answer their questions and skip them: `--spec-source`, `--harness`,
`--test-cmd`, `--workflows/--no-workflows`, and `--notifications`. For hands-free
quickstart, use `machinist onboard --yes` (or `init --yes`) — it accepts all safe
defaults and auto-enables the detected test command. Passing `--no-input`, or
running without a terminal (CI, pipes), skips every question and uses safe
defaults but does not auto-enable the test command unless `--test-cmd` is
explicit or `--yes` is used. Run `machinist --help` to see commands grouped as
`Setup`, `Tasks`, `Build`, and `Operate — daily` vs `Operate — advanced`.

`--no-workflows` is the explicit externally-managed mode. It writes
`github.manage_workflows: false`, skips workflow generation, and makes
`doctor` report that managed drift checking is disabled. To return ownership to
AgentMachinist, set that field to `true`, run `machinist sync-workflows`, review
the generated files, and commit them.

If you skipped the test-gate question or ran non-interactively, set a real test
Gate before committing, for example with
`machinist config set tests.command "uv run pytest"`:

```yaml
tests:
  command: uv run pytest
```

Review and persist the setup before the first Task. With `--setup-pr`, the
controller already committed and pushed the managed files: review and merge
the returned draft PR. With plain `machinist onboard`, review and commit the
generated files yourself, then deliver the commit to the default branch through
your repository's normal process:

```sh
git status --short
git add machinist.yaml .machinist/specs/.gitkeep .gitignore
git add .github/ISSUE_TEMPLATE/agentmachinist-task.yml
git add -p .github/workflows   # review each hunk
git diff --cached              # verify what will be committed
git commit -m "chore: configure AgentMachinist"
git push
```

If your repository already ignored `.machinist/runs/`, the initializer leaves
that rule unchanged. Omit an unchanged `.gitignore` from the staged files. Do
not commit anything until the staged diff matches the configuration you intend
to run.

After setup lands on the default branch, switch back to that branch and update
your checkout, then verify the deployed integration:

```sh
git switch <default-branch>
git pull --ff-only
machinist doctor --run-gates
```

`doctor --run-gates` checks config, labels, deployed workflows, the sealed issue
form, and Verification Gates, and prints remediation for each `FAIL`. Resolve
every `FAIL`. Treat a warning that no Gates are configured as an explicit
decision. The approval workflow must exist on GitHub's default branch before a
comment or label can record SHA-bound Approval. See the [visual
guide](https://agentmachinist.vinny.dev/first-run-guide.html) for the foreground
local alternative.

## Your first GitHub issue Task

For the local journey, use `machinist start` as shown above. The following
commands create Tasks for the existing GitHub integration.

Create a focused Task; creation validates the body before opening the issue:

```sh
machinist task new --title "Make authentication recovery actionable"
```

The managed form captures objective, acceptance checkboxes, constraints,
verification, and context. `task new` creates an unlabeled issue by default;
the completion guidance prints the next command using the created
issue. With `github.spec_source: local`, generate its Spec directly:

```sh
machinist spec 7
```

With `github.spec_source: github-actions`, the completion instead prints a
`gh issue edit` command with the issue URL and configured trigger label to
start hosted Spec generation. Follow it with the printed `machinist explain 7`
command to check progress.

Add `--dispatch` during creation to apply the trigger label after validation:

```sh
machinist task new --title "Add export recovery" --dispatch
```

With `github.spec_source: local`, the completion points to one watcher pass
to process queued Tasks:

```sh
machinist watch --once -v
```

If a watcher is already running, use `machinist explain 7` to check progress.
With `github.spec_source: github-actions`, the hosted workflow owns Spec
generation. Follow the printed command to check the issue while it runs:

```sh
machinist explain 7
```

Use `--body-file task.md` or `--body-file -` on `machinist task new` for file
or stdin input. Invalid input and failed creation preserve a draft and print a
recovery command. Required sections accept `##` and GitHub issue forms' `###`
headings; deeper headings stay within their field. Objectives need at least six
words, and acceptance checkboxes cannot be empty or placeholder text.
Use `machinist task lint 7` to recheck an issue after editing its body.

For local Spec dispatch, you can address a specific issue directly without a
trigger label:

```sh
machinist spec 7 --dry-run
machinist spec 7
```

`--dry-run` is a read-only preview: it prints the proposed Spec without a
commit, push, or PR. The normal command reads issue 7, provisions an isolated
workspace, runs the harness in its spec mode, rejects repository mutations,
writes `.machinist/specs/issue-7-spec.md`, commits and pushes `agent/issue-7`,
and opens a draft PR.
Its completion output points to the draft PR for human Spec review and prints
`machinist approve --issue 7` as the action to take after reading it.

## Review and approve

Local Tasks use `machinist approve --task T1 --spec-sha <full-spec-commit-sha>`.
The supplied SHA must match the saved Spec; Approval continues local Execute
and Review. Local Review is advisory and does not authorize integration.

For the GitHub issue workflow, use the trusted workflow Approval below.

Read the spec in the draft PR. Approval records both the configured label and
the exact 40-character PR head SHA. Choose one method:

```sh
machinist approve --pr 8
# or: machinist approve --issue 7
```

GitHub `approve` takes exactly one of `--pr` or `--issue`; local Tasks use the
separate `--task` selector. There is no ambiguous positional Approval target.

Or post the exact PR comment:

```text
/machinist-execute <full-spec-commit-sha>
```

Copy the full SHA-bound command from the Spec PR body. The managed workflow
first requires write or admin access and refuses the approval if the PR head
changed before the job ran. Applying the configured
`machinist:approved` label manually binds the approval to the head SHA carried
by that label event, so a queued force-push cannot silently authorize new code.
Both paths require write or admin access, because association and label
permission can be weaker than push authority. Either way the approver's login
is recorded on the approval comment. `machinist approve` requests this workflow
transition; `machinist explain 7` remains `awaiting approval` until the workflow
has verified and recorded it. `approval pending` means a label is already
visible without trusted SHA Evidence, as can happen briefly on the manual-label
path.

Wait for `machinist explain 7` to report `approved` before starting Execute.
The Approval completion prints that check and explains the wait.
The command reads the legacy GitHub pipeline even when this checkout also has
local Tasks; plain `machinist status` selects local Tasks in that case.

```sh
machinist explain 7
```

Repeat that read until it reports `approved`, then run Execute:

```sh
machinist run 7
```

After Execute succeeds, run `machinist review 7` if `review.enabled` is true.

Alternatively, leave `machinist watch` running; it waits for valid Approval and
dispatches eligible Phases. An early manual `run` can persist a failed Execute.
After Approval is recorded, recover that failure with
`machinist retry 7 --phase execute --run`, then complete Review when enabled.

If anyone changes the spec branch afterward, `machinist explain 7` reports
`approval stale` and execution refuses. Approve the new head again.

To regenerate a successful Spec from the current issue on its existing branch
and draft PR, run:

```sh
machinist spec 42 --revise
```

Review the regenerated diff and approve its new head. `machinist retry` remains
the recovery command for failed attempts; it is not the successful-Spec
revision path.

If the Spec should be rejected instead, abandon it explicitly:

```sh
machinist spec 42 --abandon --reason "requirements changed"
```

`--reason` is optional. Abandonment records the outcome, removes the issue's
trigger label and the PR's approval label, and closes the open draft PR. It
does not merge or delete the branch.

Do not mark the draft ready yourself. AgentMachinist uses that transition to
signal completed implementation and configured Verification Gates, plus
independent Review when `review.enabled` is true. Disabling Review in the
legacy GitHub workflow means a ready PR has no independent Review guarantee.

When `review.enabled` is true, Execute leaves the implementation draft. Review
checks the exact delivered head in read-only mode against the approved Spec,
diff, and verification evidence; it posts versioned structured findings and
then marks the PR ready. Findings are advisory in this release: they do not
trigger an autonomous fix or merge. Successful Review prints a command to open
the PR so you can inspect its diff and report before deciding whether to merge.
If Review fails or the head changes, the PR stays draft:

```sh
machinist review 42
machinist retry 42 --phase review --run
```

`--run` performs the retry immediately; without it, retry only makes the failed
Phase eligible for a later command or watcher pass.

`machinist run <issue> --force` is an intentional rework path for a ready PR.
It does not bypass immutable approval: approve that PR's current head again
before forcing a second implementation attempt.

For feedback-driven rework, prefer the explicit amendment command. The ready
PR's current head still needs fresh approval, and exactly one feedback source
is required:

```sh
machinist approve --issue 42
```

Wait for the approval workflow to succeed, then inspect its trusted Evidence:

```sh
machinist inspect 42 --json
```

Confirm that the GitHub PR source's `approval_sha` matches its full `head_sha`.
The ready PR remains `in review`, so do not wait for an `approved` pipeline
state here. Once the current head's Approval is recorded, run the amendment:

```sh
machinist amend 42 --feedback "Keep the public API; add the missing edge-case test."
# or: machinist amend 42 --feedback-file review-notes.txt
```

Legacy GitHub amendment provisions a fresh Execute attempt from the approved
remote head; it does not generate a new Spec or import manual edits from a
retained failed Workshop. A new successful Execute SHA can receive a fresh
Review with new Evidence and findings. A completed Review of the same SHA
cannot run again. Local `amend --task T1` instead generates a new Spec and stops
for its fresh exact-SHA Approval.

## GitHub Spec generation: local or CI

`github.spec_source` assigns the legacy GitHub Spec Phase to exactly one
dispatcher. Its `local` value is distinct from the foreground `T1` workflow;
GitLab support does not include a hosted Spec dispatcher:

```yaml
github:
  spec_source: local
```

- `local` means `machinist watch` generates specs using your local harness
  login. The managed spec workflow is absent.
- `github-actions` means `watch` leaves labeled issues alone and
  `machinist-spec.yml` owns spec generation. Run `machinist sync-workflows`
  after changing the value.

`github.spec_install` chooses how CI obtains the controller: `pypi` (default)
pins `agentmachinist==<installed version>`; `checkout` runs `uv sync --frozen`
and `uv run machinist spec` from the repository (for dogfooding AgentMachinist
itself). The generated workflow reads the selected Spec adapter's CI metadata.
Built-ins install pinned Claude Code, Codex, OpenCode, or Pi packages and bind
only their declared repository secret (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
or `GEMINI_API_KEY`). `github.spec_secret_env` overrides the secret name, not
its value. A plugin without a CI Spec profile must use local dispatch.

## Configuration reference

Both workflows use the same strict configuration schema. Commands default to
root `machinist.yaml`; foreground Tasks read the saved local file. Inspect or
change that file explicitly:

```sh
machinist config show --path .machinist/runs/local/config.yaml
machinist config validate --path .machinist/runs/local/config.yaml
machinist config set tests.command "uv run pytest" --path .machinist/runs/local/config.yaml
```

Use `tests.command` only for the single-Gate form; if named Gates are present,
edit `verification.gates` instead. Local loading additionally requires at least
one required Gate, Review enabled, local Spec source, managed workflows off,
telemetry endpoint unset, and an absolute Workshop root outside the repository.
Generic `config validate` checks the shared schema, not all local constraints.
Changing `github.repo` does not choose a local publication target: that target
comes from origin plus the explicit `publish --provider` selection.

What `machinist onboard` writes to disk is intentionally minimal — about 20 lines:

```yaml
# AgentMachinist configuration
# See docs/getting-started.md for all options.
version: 1

harness:
  name: claude-code

tests:
  command: null  # or "uv run pytest" when detected / --test-cmd is used

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

review:
  enabled: true
```

`machinist config show` renders the effective config with all defaults. The full
reference below shows every key you can set — unknown keys fail validation:

```yaml
version: 1

harness:
  name: claude-code
  command: null
  model: null
  extra_args: []
  timeout_minutes: 30
  spec_timeout_minutes: 10
  spec: null
  execute: null
  review: null

instructions:
  spec:
    paths: []
    append: null
  execute:
    paths: []
    append: null
  review:
    paths: []
    append: null

review:
  enabled: true

github:
  repo: null
  spec_source: local
  spec_install: pypi
  manage_workflows: true
  spec_secret_env: null
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

verification:
  gates: []
  harness_may_run_gates: true

telemetry:
  otlp_endpoint: null
  timeout_seconds: 5

queue:
  max_tasks_per_pass: 1
  allowed_hours: null
  task_budget: null

notifications:
  backend: desktop
  events: [failure]
  command: null
  webhook: null

limits:
  max_issue_body_chars: 50000
  max_spec_chars: 100000
  max_changed_files: 100
  max_changed_bytes: 5242880
  denied_paths: [.machinist]
  allow_binary: false
  allow_test_deletions: false
```

Unknown keys fail validation. In the legacy GitHub workflow, with `repo: null`,
AgentMachinist derives and binds the exact GitHub host, owner, and repository from the controller's Git
origin; an explicit `repo` must match it. `workspace.strategy` is `worktree` by
default, which is fast because the Workshop shares your repository's object
store. It also shares `config`, `hooks/`, and `info/` with your checkout, so
the Git metadata custody guard watches files you edit yourself, and installing
a hook while a Task runs will stop that Task. Choose `clone` when you want each
Workshop to own its Git metadata; see
[the trust model](trust-model.md) for what the guard covers. Workspace cleanup
can be `always`, `on_success`, or `never`;
keeping failed workspaces is useful for diagnosis. `tests.command: null` skips
the legacy command; verification is skipped only when no named
`verification.gates` are configured, which is surfaced as a legacy doctor
warning. Foreground local Tasks reject a configuration without a required Gate.
The later sections describe the phase profiles, named gates, admission
controls, notifications, and safety limits.

Use the config commands before starting a Task:

```sh
machinist config validate
machinist config show
machinist config show --json
machinist config schema --output machinist.schema.json
machinist config set queue.max_tasks_per_pass 2
```

`config show` resolves the effective Spec, Execute, and Review harness profiles.
`config set` parses its value as YAML, validates the complete document, writes
atomically, and rewrites the file as canonical YAML; comments are normalized.
`validate`, `show`, and `set` accept `--path` for a non-default config file.

For the GitHub integration, after changing labels, dispatcher ownership, or
the installed package version, regenerate workflows:

```sh
machinist sync-workflows
git diff -- .github/workflows
```

## Phase harnesses and instruction overlays

The top-level harness remains the default for all phases. Override only the
fields that differ for Spec, Execute, or Review:

```yaml
harness:
  name: claude-code
  model: null
  extra_args: []
  timeout_minutes: 30
  spec_timeout_minutes: 10
  spec:
    model: claude-sonnet-4-5
    timeout_minutes: 12
  execute:
    name: codex
    model: gpt-5.3-codex
    timeout_minutes: 45
  review:
    name: claude-code
    model: claude-sonnet-4-5
    timeout_minutes: 12
```

Omitted fields inherit from the top level. When a phase changes harness
provider, its command, model, and extra arguments do not accidentally inherit
from the other provider. Adapter-owned sandbox, permission, model, session,
and tool flags are rejected from `extra_args`.

Repository-specific guidance can be appended independently to each prompt:

```yaml
instructions:
  spec:
    paths: [docs/product-rules.md]
    append: "Preserve the public CLI contract."
  execute:
    paths: [AGENTS.md, docs/testing.md]
    append: null
  review:
    paths: [AGENTS.md, docs/review-policy.md]
    append: "Report findings; never edit files."
```

Paths are ordered, UTF-8, repository-relative files. AgentMachinist rejects
path escapes, duplicate resolutions, NUL bytes, excessive files, and an
oversized combined overlay before invoking a harness.

## Verification gates and change limits

For one legacy gate, keep `tests.command`. For ordered, separately reported
checks, set `tests.command: null` and configure named gates instead:

```yaml
tests:
  command: null
verification:
  gates:
    - name: unit tests
      command: uv run pytest -q
      timeout_minutes: 30
      required: true
      mutation_policy: forbid
    - name: advisory dependency report
      command: ./scripts/dependency-report.sh
      timeout_minutes: 5
      required: false
      mutation_policy: forbid
limits:
  max_issue_body_chars: 50000
  max_spec_chars: 100000
  max_changed_files: 100
  max_changed_bytes: 5242880
  denied_paths: [.machinist, secrets]
  allow_binary: false
```

Required command failures block candidate delivery and PR readiness. Ordinary
advisory command failures are preserved as Evidence without blocking delivery,
and advisory
gates must be read-only. Cancellation, forbidden working-tree mutation, or an
inability to take the before/after snapshot always blocks fail-closed,
regardless of `required`. Configure either named gates or `tests.command`,
never both. Execute also enforces changed-file, changed-byte, denied-path, and
binary-file limits before the controller commits or pushes.

By default (`verification.harness_may_run_gates: true`) the implementation
prompt also lists the gate commands and asks the harness to run each required
gate itself and iterate until it passes before finishing — fixing the code,
never weakening a test. For `claude-code`, whose headless edit mode otherwise
denies command execution, those commands and added-argument variants are
allowlisted. The controller still runs every gate afterwards; the harness's own runs only
improve first-pass quality. Set `harness_may_run_gates: false` to keep the
gate commands out of the generated prompt and omit these Claude allow rules.
Other Execute adapters already permit command execution; this setting does
not prevent a Harness from discovering and running tests itself. Gate runs by
the harness count against `harness.timeout_minutes`, so budget the timeout for at least one
full gate cycle.

As the deterministic backstop for "fix the code, never the tests", Execute
refuses to commit an implementation that deleted a test file (matched by
common path heuristics: `tests/`, `test/`, `__tests__/`, and `spec/`
directories, plus `test_*`, `*_test.*`, `*.test.*`, `*.spec.*`, and
`conftest.py` basenames). Renaming a test file appears as a deletion and is
also refused. When an approved Spec legitimately removes or renames tests,
set `limits.allow_test_deletions: true` for that run and turn it back off
afterwards. Modified tests are not flagged — updating tests is normal Spec
work — so weakened assertions still need human review in the local diff or PR/MR.

## Choosing a harness

Set `harness.name` to `claude-code`, `opencode`, `pi`, or `codex` in the
configuration used by your chosen workflow. Runs
reuse the provider authentication already available to that executable.

```yaml
harness:
  name: codex
  command: /opt/homebrew/bin/codex
```

Support is not identical. Some spec modes have a CLI-enforced read-only tool or
sandbox boundary; OpenCode's plan agent is advisory. All implementations are
checked afterward for Harness-created commits and `.machinist/` changes.
Local Phases check controller/Workshop HEAD, branch, refs, and custody without
contacting a forge; legacy GitHub Phases also check live remote branch changes. See the [harness matrix](harnesses.md) and
[trust model](trust-model.md) before unattended operation.

Installed Python distributions may add a trusted adapter through the
`agentmachinist.harnesses.v1` entry-point group. The entry-point name must
match its lowercase adapter identifier; built-in names are reserved. Discovery
isolates broken plugins and `doctor` reports their failures. See
[Harness support](harnesses.md) for the version-1 descriptor and CI contract.

`harness.model` selects an adapter-specific model. `harness.extra_args` appends
arguments to Spec, Execute, and Review invocations after AgentMachinist's own
arguments. AgentMachinist rejects adapter-owned sandbox, permission, model,
session, and tool flags, including duplicate forms that could override its
controls. Other additional arguments remain advanced and may change behavior
as harness CLIs evolve, so keep the list empty unless you have reviewed the
final command and its trust impact.

## Watch admission and operator controls

These controls govern the legacy GitHub watcher, not foreground local Tasks.
Preview eligibility and every deferral reason without dispatching work:

```sh
machinist watch --dry-run
machinist watch --once --max-tasks 1
```

`queue.max_tasks_per_pass` is the persistent per-poll limit; `--max-tasks`
overrides it for one invocation (`0` admits none). You can also limit dispatch
to a time window and daily budget:

```yaml
queue:
  max_tasks_per_pass: 1
  allowed_hours:
    start: "08:00"
    end: "20:00"
    timezone: America/Chicago
    days: [mon, tue, wed, thu, fri]
  task_budget:
    max_runs_per_day: 5
    max_runtime_minutes_per_day: 240
    timezone: America/Chicago
```

Allowed-hour windows may cross midnight. `max_runs_per_day` counts Phase
attempts: Spec, Execute, and Review each consume one Task Run. The legacy
`max_tasks_per_day` key still loads with those same semantics; conflicting old
and new values fail validation, and effective config emits the canonical key.
Daily counts come from local Task Run history, so these are conservative local
watcher admission controls, not distributed quotas across several Macs or
spending caps on `start`/`approve --task`.

Pause all new dispatches or defer one issue durably:

```sh
machinist queue pause --reason "traveling"
machinist queue show --json
machinist queue defer 42 --reason "waiting for API decision"
machinist queue allow 42
machinist queue resume
```

Pause and defer do not terminate a Task that is already running. To request a
cooperative stop and block future dispatch, use cancellation:

```sh
machinist cancel 42 --reason "requirements changed"
machinist cancel 42 --clear
```

Supervised harness and verification processes receive the cancellation and are
terminated as a process group. The durable marker keeps the watcher from
starting the issue again. Clear it directly, or use an explicit
`machinist retry` after the cancelled run; retry marks the run retryable and
clears its cancellation marker.

## Notifications and safety limits

Notification delivery belongs to the legacy GitHub command/watcher workflow.
Foreground local commands print progress and results in the terminal.
Notifications are best-effort and never turn a successful Task into a failure.
Choose events from `failure`, `spec_ready`, `approval_stale`, and `pr_ready`:

```yaml
notifications:
  backend: desktop
  events: [failure, spec_ready, approval_stale, pr_ready]
  command: null
  webhook: null
```

Successful deliveries are deduplicated for 24 hours in the repository-local
`.machinist/runs/notification-ledger.json`. This survives short-lived
`watch --once` processes, including the managed LaunchAgent, while allowing a
reminder after the window. Failed, disabled, or filtered deliveries are not
recorded. If the bounded ledger is corrupt or unavailable, delivery fails open
with a warning so an important alert is not silently lost.

The other backends are `disabled`, `command`, and `webhook`. Command delivery
uses an argv list without a shell and sends event JSON on standard input.
Webhook configuration names environment variables for the URL and optional
authorization value; it does not put secrets in `machinist.yaml`.

```yaml
notifications:
  backend: command
  events: [failure, pr_ready]
  command:
    argv: [/absolute/path/to/notifier, --stdin-json]
    timeout_seconds: 5
  webhook: null
```

For a webhook, set `backend: webhook`, set `command: null`, and configure
`webhook.url_env`, optional `webhook.authorization_env`, and
`webhook.timeout_seconds`. Export the named values in the watcher's process
environment rather than committing them. A configured authorization value is
sent only to an HTTPS URL; authenticated plaintext HTTP is rejected. Webhook
redirects are rejected before another request can receive the payload or
authorization. The managed LaunchAgent intentionally uses a minimal environment
and does not copy arbitrary secrets from your shell; use desktop delivery there
unless you deliberately provision the named launchd environment values outside
AgentMachinist.

## macOS watcher service

On macOS, install, register, and immediately start a per-repository LaunchAgent
after the config and executable are ready:

```sh
machinist service install
machinist service status
machinist service logs --lines 100
```

The service runs `machinist watch --once` at
`github.poll_interval_seconds`, uses the repository as its working directory,
and writes logs under `.machinist/runs/service/`; `service logs --lines` prints a
bounded recent tail rather than following indefinitely. Lifecycle commands are
explicit:

- Start the installed service: `machinist service start`.
- Restart it: `machinist service restart`.
- Stop it: `machinist service stop`.
- Remove it: `machinist service uninstall`.

`stop` preserves the installed plist and logs. `uninstall` removes the plist
but deliberately preserves logs. `status` reports launchd registration, the
last completed watcher poll, health, and active Task Runs. Install, restart,
stop, and uninstall refuse to interrupt an active Claim; wait for it to finish
or pass `--force` only when termination is intentional. Service management
currently supports macOS launchd only.

## Local evidence and repository portfolio

For foreground local Tasks, use `machinist status T1 --json`,
`machinist status T1 --watch`, and the printed report path under
`.machinist/runs/local/tasks/`. With local configuration present, plain `status`
lists these Tasks; its `--local` flag does not switch back to legacy records.

The following read model and portfolio commands inspect the locally persisted
**legacy GitHub issue runs** under `.machinist/runs/`. They do not aggregate
nested `T1` history. In a checkout without local configuration, use:

```sh
machinist status --local --json
machinist status --watch --interval 2
machinist runs --issue 42 --json
machinist inspect 42 --offline --json
machinist explain 42 --json
machinist report --since 30d --json
```

These reports include current and historical attempts plus orphaned, partial,
or corrupt runtime artifacts; remote-source errors do not erase readable local
evidence.

`explain` is side-effect free and resolves the Task's current state, next
command, phase profiles, gates, workspace policy, limits, queue state, attempts,
and allowed credential names—never credential values. `status --watch` emits
only changed snapshots; `--json` produces one compact JSON object per line.

`report` aggregates outcomes, retries, cancellations, duration percentiles,
verification failures, and safe Harness/model metadata from local JSONL
history. Export is disabled unless `telemetry.otlp_endpoint` or an explicit
`--otlp-endpoint` is supplied. OTLP/HTTP JSON contains aggregate repository,
phase, status, Harness, and model attributes only. Set authorization in
`MACHINIST_OTLP_AUTHORIZATION`, never in `machinist.yaml`.

For several repositories on one Mac, maintain the optional registry and view
their local status together:

```sh
machinist repo add /absolute/path/to/project
machinist repo list --json
machinist status --all --json
```

To unregister a repository later, run `machinist repo remove /absolute/path/to/project`.

Portfolio status reads local legacy issue-run Evidence without GitHub requests.
It reports an unavailable repository alongside
healthy ones instead of failing the entire view.

## Troubleshooting

For a foreground Task, start with `machinist status T1`; follow its printed
`Next:` command. Use `machinist config show --path .machinist/runs/local/config.yaml` to
inspect the local settings and the [local recovery guide](local-workflow.md#amend-or-recover)
for retry, amendment, integration, or publication problems. Plain `doctor`
checks GitHub setup.

**New in 0.15.0:** optional local readiness is available with
`machinist doctor --local` or `machinist doctor --local --json`. It adds no
required onboarding step. It checks Git state and identity, the local configuration that `start` would use,
Harness availability and supported version/help/authentication probes, and
required Gate command entry points. By default it creates no Task, Workshop,
runtime/config file, exclusion, or ref, and invokes no model, forge, update
probe, or Verification Gate. A supported auth probe does not establish model
access or quota.

`machinist doctor --local --run-gates` explicitly runs the configured Gates in
the controller checkout after readiness checks pass. Those commands can write
or download; passing them does not prove the isolated Workshop baseline that
`start` still checks. See [optional local readiness](local-workflow.md#optional-local-readiness).

For the legacy GitHub issue workflow, start with:

```sh
machinist doctor --run-gates
machinist explain 7
machinist inspect 7
```

`machinist status` also shows the legacy pipeline when the checkout has no
local configuration; otherwise it lists local Tasks.

Common states and responses:

| State or error | Response |
| --- | --- |
| `awaiting spec` | Run local `watch`/`spec`, or verify the CI dispatcher. |
| `awaiting approval` | Review the draft spec. |
| `approval pending` | The label exists but SHA evidence has not been recorded; inspect the approval workflow, then approve again only if it failed. |
| `approval stale` | The branch changed after approval; approve the current head. |
| `approved` | Run `machinist run <issue>` or leave `watch` running. |
| `awaiting review` | Leave `watch` running or run `machinist review <issue>`. |
| `in review` | Implementation finished; review the PR. |
| `spec running` / `execute running` / `review running` | AgentMachinist holds the Claim; `status`/`runs` show its current named stage and elapsed time. |
| `spec interrupted` / `execute interrupted` / `review interrupted` | No process holds the recorded Claim; run the exact `Next:` retry command. |
| `spec failed` / `execute failed` / `review failed` | Inspect the retained Evidence, fix the cause, then use the displayed retry command. |
| `spec cancelled` / `execute cancelled` / `review cancelled` | Clear or replace the cancellation request before retrying. |
| `spec abandoned` / `execute abandoned` / `review abandoned` | The operator ended this lifecycle; retry only after deciding it should resume. |
| `spec closed` | The Spec PR is closed; revise the Task intent before starting another Spec. |
| Failed Execute run retained useful edits | Inspect the path, then run `machinist retry <issue> --phase execute --run --resume` from the repository root. |
| Failed Execute run should start clean | Run `machinist retry <issue> --phase execute --run --fresh`. Omitting both recovery flags also selects a fresh attempt. |
| Task should not start again | Run `machinist cancel <issue> --reason "..."`; clear it directly or explicitly retry only when dispatch is safe. |
| Queue or issue is intentionally waiting | Run `machinist queue show`; use `queue resume` or `queue allow <issue>` as appropriate. |
| Workspace already exists | Inspect it first, or prune it with `machinist clean --issue <issue>` or `machinist clean --all`. |
| Remote base fetch fails | Confirm origin and the repository's current default branch; a new remote Task will not use a stale tracking ref for a deleted branch. |
| Managed workflow drift | `watch` and `update-check` report it. Run `machinist sync-workflows`, inspect, commit, and push. |
| Configuration is unclear | Run `machinist config validate` and `machinist config show`; neither starts a Task. |
| GitHub is unavailable | Read legacy Evidence with `machinist runs` or `machinist inspect <issue> --offline`; `machinist status --local` also works when no local Task configuration is present. |
| launchd watcher is quiet | Run `machinist service status` and `machinist service logs --lines 100`. |
| Unsure whether the CLI is current | Run `machinist update-check`; it prints the upgrade command for this installation and flags managed-workflow drift. |
| `doctor` warns that PyPI is unreachable | The update probe is advisory. Set `MACHINIST_NO_UPDATE_CHECK=1` on offline machines. |

Task Run failures and recovery evidence live under `.machinist/runs/`. The
initializer adds `/.machinist/runs/` to `.gitignore`, and `doctor` fails its
runtime-state check if that protection is later removed. A failed implementation
is not silently retried. Checkpoints preserve evidence about partial push
progress, and `--resume` validates and reuses the retained managed workspace.
`--fresh` starts another workspace from the approved head. Fresh is the default
when neither recovery flag is supplied. Run retry from the repository root, not
from inside the retained workspace.

## Operational limits

- Local file locks prevent duplicate work on one machine/process family; they
  are not a distributed lock across multiple hosts. Run one local watcher per
  repository.
- Queue windows and daily budgets are GitHub watcher admission controls based
  on local issue history; they do not coordinate several hosts or govern
  foreground local Tasks.
- Harness processes run as your OS user. AgentMachinist reduces controller
  credentials and checks postconditions, but it is not a container or VM.
- Provider authentication, quotas, model behavior, and spec quality remain
  external dependencies.
- Foreground Tasks produce a reviewed local candidate. Explicit `integrate`
  permits a clean exact fast-forward; `publish` optionally creates or updates
  a GitHub PR or GitLab MR. Legacy GitHub automation produces a ready PR.
  AgentMachinist does not merge remotely, deploy, or verify a deployed runtime.
- Local orchestration requires no forge. Offline inference separately requires
  a compatible local provider, downloaded models and dependencies, and a run
  verified with network access denied.

For restarts, retries, logs, and cleanup, continue with the
[operator runbook](operator-runbook.md).

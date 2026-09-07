# AgentMachinist

AgentMachinist takes a small development Task from intent to a reviewed local
change. It coordinates Claude Code, OpenCode, Pi, or Codex, with human Approval
of the exact Spec before implementation and human review before integration.
GitHub and GitLab are optional sources of Tasks and destinations for publication.

```text
Task → Spec commit → human Approval → implementation → verification → Review
                                                                    │
                                  human review → local integration ◄─┘
                                  optional GitHub PR / GitLab MR
```

The controller owns commits, Task records, and optional publication. Local
integration is an explicit fast-forward operation into your clean base checkout.
AgentMachinist never merges remotely or automatically.

Current release: [AgentMachinist 0.14.0 on PyPI](https://pypi.org/project/agentmachinist/0.14.0/).

## Install

```sh
uv tool install agentmachinist
```

You also need `git` and one supported Harness executable (`claude`, `opencode`,
`pi`, or `codex`). GitHub operations require authenticated [`gh`](https://cli.github.com);
GitLab operations require authenticated [`glab`](https://docs.gitlab.com/cli/).
The core
CLI is tested on macOS and Linux with Python 3.12–3.14. Managed background
service commands are macOS-only; on Linux, schedule `machinist watch --once`
with your existing service manager.

`machinist update-check` compares the installed release against PyPI and
prints the upgrade command for how this copy was installed (`uv tool`, `pipx`,
`pip`, or a source checkout). `machinist doctor` reports the same result as a
diagnostic row. Set `MACHINIST_NO_UPDATE_CHECK=1` to suppress both probes on
offline or CI machines.

Upgrading the package is not always the whole upgrade. Managed workflows are
projected files, so a workflow change only takes effect once you run
`machinist sync-workflows`. `machinist watch` reports that drift at startup and
`machinist update-check` reports it alongside the release comparison, so you do
not have to run `doctor` to find out. The advisory never blocks a command and
never appears in `update-check --json`.

## Start

Version 0.14.0 includes the guided local workflow and optional GitLab support.
If you already installed AgentMachinist with uv, upgrade with
`uv tool upgrade agentmachinist`. The existing GitHub workflow remains available.

```sh
cd your-repository
machinist start "Handle an invalid timezone without crashing" --test-cmd "uv run pytest"
# Read the saved Spec and copy the exact Approval command printed by start:
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
# Approval continues implementation, verification, and independent Review.
machinist status T1
# Inspect the diff and report, then integrate explicitly:
machinist integrate T1
```

First start detects an installed Harness and a verification command. Its local
settings and Task records live under `.machinist/runs/local/`, excluded through
Git's local exclude file. It does not require an origin, labels, a daemon, forge
authentication, or hosted workflows. Existing `machinist.yaml` settings remain
available. Local orchestration can still use a cloud model; offline inference
requires a separately configured local provider.

Verification runs in an isolated committed checkout; dependency folders from
your working repository are not copied. Use a self-preparing command such as
`npm ci && npm test` or `uv run pytest`. A failing baseline stops before the
Spec Harness. Correct the Gate in `.machinist/runs/local/config.yaml` or its
dependency setup, then run `machinist retry --task T1 --phase spec`.

You can publish the same reviewed candidate when collaboration is useful:

```sh
machinist publish T1 --provider gitlab
# Or: machinist publish T1 --provider github
```

Publication requires an origin that matches the selected forge and authenticated
CLI. It preserves local work on failure and retries the same branch and change
request without repeating Harness work or verification. GitLab support includes
nested projects and explicitly bound self-managed hosts; it covers issue intake
and merge-request publication, not GitLab-hosted Spec automation.

See the [local workflow guide](docs/local-workflow.md) for input files, external
issues, amendments, recovery, and use by a solo developer or small team.

## GitHub automation

Existing GitHub issue commands, trusted workflow Approval, and watcher operation
remain available. Configure that integration separately:

```sh
cd your-repository
machinist onboard
# Answer the setup questions, review the generated files, then:
git status --short
git add machinist.yaml .machinist/specs/.gitkeep .gitignore
git add .github/ISSUE_TEMPLATE/agentmachinist-task.yml
git add -p .github/workflows   # review each hunk
git diff --cached              # verify what will be committed
git commit -m "chore: configure AgentMachinist"
git push
machinist doctor --run-gates   # after setup reaches the default branch
machinist watch
```

`machinist doctor --run-gates` is the single health check — it already verifies
labels, workflow drift, the sealed issue form, and verification gates, and prints
the exact fix for any `FAIL`. Only run the individual
`machinist sync-labels --check`, `machinist sync-workflows --check`, or
`machinist task template --check` if doctor asks for them.

Use `machinist onboard --setup-pr` when you want AgentMachinist to put only its
managed setup files on a pushed `chore/agentmachinist-setup` branch and open a
draft PR. Fresh setup requires a clean default branch; a recognized partial
setup resumes without replacing your preferences. Setup checks local readiness
before publication. Merge setup, then run full doctor to verify the deployed
workflows. `machinist rehearse` exercises the production local Phases, real Git,
verification, and integration with a fake Harness; `--harness` explicitly opts
into configured providers in the disposable repository.

In a terminal, `machinist onboard` (the GitHub setup entry point) asks a short
set of setup questions — dispatch mode, managed workflows, harness, test gate,
and notifications — each with a one-line explanation and a safe default. Flags
such as `--harness`, `--test-cmd`, `--spec-source`, and `--notifications`
pre-answer their questions; `--yes` accepts all safe defaults and auto-enables the
detected test command for hands-free quickstart; `--no-input` (or a non-interactive
shell) also skips questions but does not auto-enable the test command unless you
pass `--test-cmd`. Errors outside a configured repo now point you to `machinist
onboard` and the first-run guide. `machinist init` is the same setup step without
the guided receipt — prefer `onboard` for new repositories. `machinist --help`
groups commands as `Setup`, `Tasks`, `Build`, and `Operate — daily` vs `Operate — advanced`.

Review the staged diff before committing. The managed Task form is installed
even when Actions workflows are externally managed. Managed workflows must be
pushed before GitHub comment or label approval can record SHA-bound evidence.
`machinist init` also adds `/.machinist/runs/` to `.gitignore`. If you manage
workflows yourself, `machinist init --no-workflows` records
`github.manage_workflows: false`; `doctor` then reports that its drift check was
intentionally skipped.

The default `github.spec_source: local` makes `watch` own spec generation.
Choose `github-actions` and run `machinist sync-workflows` if CI should own that
phase instead. Exactly one source is active, preventing duplicate spec runs.
Managed CI installs the selected Spec adapter and reads its declared secret
name; built-ins support Claude Code, Codex, OpenCode, and Pi.

Approval is bound to the exact PR head commit. Use either:

```sh
machinist approve --issue 57
# or: machinist approve --pr 18
# or post the SHA-bound comment shown in the Spec PR body:
# /machinist-execute <full-spec-commit-sha>
```

GitHub Approval takes exactly one of `--issue` or `--pr`; local Approval instead
uses `--task T1 --spec-sha <sha>`. These selectors keep local Tasks, issues, and
pull requests distinct.

Editing the spec after approval makes that approval stale and blocks execution
until the new head is approved.

With the starter configuration, a successful Execute run leaves the PR draft
for an independent read-only Review Task Run. Review compares the approved
Spec, diff, and verification evidence, posts a structured advisory report, and
alone marks the exact implementation head ready. Findings never trigger an
automatic repair or merge. Use `machinist review <issue>` manually or
`machinist retry <issue> --phase review` after a failed Review.

The CLI approval command submits the SHA-bound comment; the managed GitHub
workflow independently verifies the current head and approver's write access,
then records the marker and label. Until that workflow completes, `status`
remains `awaiting approval` rather than pretending execution is authorized.
`approval pending` specifically means the label is visible but trusted SHA
Evidence has not arrived yet.

Revise or explicitly abandon a successful Spec by issue number:

```sh
machinist spec 42 --dry-run
machinist spec 42 --revise
machinist spec 42 --abandon --reason "requirements changed"
```

The dry run prints the proposed Spec without commits, pushes, or a PR. Revision
regenerates the Spec on its existing branch and draft PR. Abandonment records
the reason, removes the trigger and approval labels, and closes the open draft
PR.

## Commands

| Command | Purpose |
| --- | --- |
| `machinist start [<objective>] [--body-file <path>] [--from-issue <url>]` | Save a local Task, generate its Spec, and stop for exact human Approval. |
| `machinist approve --task <Tn> --spec-sha <sha>` | Approve one local Spec and continue Execute, verification, and Review in the foreground. |
| `machinist continue <Tn>` | Continue eligible local work or show the next required human action. |
| `machinist status <Tn> [--json]` | Inspect one local Task and its next action without forge access. |
| `machinist integrate <Tn>` | Explicitly fast-forward a clean local base to the exact reviewed candidate. |
| `machinist publish <Tn> --provider github\|gitlab [--host <host>]` | Publish the reviewed local candidate as a PR or MR with recoverable intent. |
| `machinist retry --task <Tn> --phase spec\|execute\|review [--fresh]` | Explicitly retry a failed local Phase in the foreground. |
| `machinist amend --task <Tn> --feedback <text>` | Regenerate the local Spec from feedback and require fresh Approval. |
| `machinist init [--yes]` | Create config, spec storage, labels, managed issue form, and workflows; asks setup questions in a terminal (`--yes` hands-free, `--no-input` skips without auto-enabling test command). |
| `machinist onboard [--setup-pr] [--yes]` | Run guided setup in place or deliver only managed setup files on a draft PR; `--yes` accepts defaults + detected test command. |
| `machinist rehearse [--harness]` | Exercise production local Phases, Git, verification, Review, and integration; paid Harness use is opt-in. |
| `machinist doctor [--run-gates]` | Run read-only setup and workflow-drift diagnostics; single health check that prints the exact fix for any `FAIL` (only run individual `--check` commands if doctor asks). |
| `machinist update-check [--json] [--timeout <seconds>]` | Compare the installed release against PyPI, print how to upgrade, and report managed-workflow drift. |
| `machinist sync-workflows [--check]` | Write or verify config-derived workflows. |
| `machinist sync-labels --check\|--apply` | Verify or create the two configured lifecycle labels. |
| `machinist config validate\|show\|schema\|set` | Validate, inspect, export, or atomically update configuration. |
| `machinist task template --write\|--check` | Project or verify the sealed GitHub issue form. |
| `machinist task new --title <title> [--body-file <path>] [--dispatch]` | Create a structured GitHub issue; preserve drafts on failure and dispatch only after lint passes. |
| `machinist task lint <issue> [--json]` | Check objective, acceptance criteria, constraints, and verification readiness. |
| `machinist spec <issue> [--dry-run]` | Preview a Spec, or generate it and open its draft PR. |
| `machinist spec <issue> --revise` | Regenerate a successful Spec on its existing branch and PR. |
| `machinist spec <issue> --abandon [--reason <text>]` | Record rejection and close the open draft PR. |
| `machinist approve [--issue <issue>\|--pr <pr>]` | Bind approval to the current PR head without number ambiguity. |
| `machinist run <issue>` | Implement an approved spec and run the test gate. |
| `machinist review <issue>` | Independently review the exact implemented draft and mark it ready. |
| `machinist amend <issue> --feedback <text>` | Rework a ready PR from explicit feedback after fresh approval. |
| `machinist cancel <issue> [--reason <text>\|--clear]` | Cooperatively stop or block an issue's dispatch. |
| `machinist watch [--once] [--dry-run] [--max-tasks <n>]` | Preview or dispatch eligible tasks continuously or once. |
| `machinist queue pause\|resume\|defer\|allow\|show` | Persist operator controls over new watcher dispatches. |
| `machinist service install\|start\|restart\|stop\|status\|logs\|uninstall` | Manage the repository's macOS launchd watcher; destructive lifecycle actions refuse active Claims unless forced. |
| `machinist explain <issue> [--json]` | Show effective policy, resolved profiles, attempts, and the exact next action without secrets. |
| `machinist status [--local\|--all] [--json]` | Show GitHub state, local Task Runs, or a registered portfolio. |
| `machinist status --watch [--interval <seconds>] [--json]` | Emit changed-only live pipeline snapshots until Ctrl-C. |
| `machinist runs [--issue <issue>] [--json]` | Read current, historical, orphaned, and corrupt local run records. |
| `machinist report [--since 30d] [--json] [--otlp-endpoint <url>]` | Aggregate local reliability metrics and optionally export allowlisted OTLP/HTTP JSON. |
| `machinist retry <issue> [--phase spec\|execute\|review]` | Re-enable one failed Task Run. |
| `machinist retry <issue> --phase execute --run [--resume\|--fresh]` | Reuse a retained workspace or start a fresh Execute attempt; fresh is the default. |
| `machinist inspect <issue> [--offline] [--json]` | Show GitHub, workspace, and complete Task Run diagnostics. |
| `machinist repo add\|remove\|list` | Maintain the optional local repository registry. |
| `machinist clean [--issue <issue>\|--all]` | List or remove retained workspaces. |

## Documentation

- [TL;DR](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/tldr.md)
- [Getting started](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/getting-started.md)
- [Local workflow and optional publication](docs/local-workflow.md)
- [GitHub visual first-run field guide](https://agentmachinist.vinny.dev/first-run-guide.html)
- [Architecture and lifecycle](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/architecture.md)
- [Operator runbook](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/operator-runbook.md)
- [Trust model](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/trust-model.md)
- [Harness support](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/harnesses.md)
- [Architecture decisions](https://github.com/vscarpenter/AgentMachinist/tree/main/docs/adr)
- [Contributing](https://github.com/vscarpenter/AgentMachinist/blob/main/CONTRIBUTING.md)
- [Changelog](https://github.com/vscarpenter/AgentMachinist/blob/main/CHANGELOG.md)

The trust model is deliberately narrower than “the agent cannot use git.”
Harness flags, credential reduction, repository postconditions, and push leases
reduce risk, but local harnesses still execute with the operating-system access
of the user who launched them. Read the trust model before unattended use.

## Releasing

Releases use PyPI Trusted Publishing. Bump `pyproject.toml`, update the
changelog, and publish a GitHub Release tagged `v<version>`. The release job
first checks tag/version equality, runs tests and workflow checks, builds both
distributions, smoke-tests the installed wheel and sdist through a generated
first-run project, and records SHA-256 hashes. A minimal job
then publishes those verified artifacts. Only after publication do separate
jobs attach the distributions and checksum file to the GitHub Release and
verify that the exact version is visible and installable from PyPI.

## License

MIT — see [LICENSE](https://github.com/vscarpenter/AgentMachinist/blob/main/LICENSE).

# AgentMachinist

AgentMachinist takes a small development Task from intent to a reviewed local
change. It coordinates Claude Code, OpenCode, Pi, Codex, or Goose, with human Approval
of the exact Spec before implementation and human review before integration.
GitHub and GitLab are optional sources of Tasks and destinations for publication.

New here? Follow [Start here](docs/tldr.md) for your first Task, or try it on
the disposable [example project](examples/first-task/README.md) first.

```text
Task → Spec commit → human Approval → implementation → verification → Review
                                                                    │
                                  human review → local integration ◄─┘
                                  optional GitHub PR / GitLab MR
```

The controller owns commits, Task records, and optional publication. Local
integration is an explicit fast-forward operation into your clean base checkout.
AgentMachinist never merges remotely or automatically.

Current release:
[AgentMachinist 0.19.0 on PyPI](https://pypi.org/project/agentmachinist/0.19.0/).

## Install

Install the controller, then enter the repository you want to work on:

```sh
uv tool install agentmachinist
machinist --version
```

You also need `git` and one supported Harness executable (`claude`, `opencode`,
`pi`, `codex`, or `goose`). GitHub operations require authenticated [`gh`](https://cli.github.com);
GitLab operations require authenticated [`glab`](https://docs.gitlab.com/cli/).
The core CLI is tested on macOS and Linux with Python 3.12 to 3.14. Managed
background service commands are macOS-only; on Linux, schedule
`machinist watch --once` with your existing service manager.

## Start

Start on a clean named branch with an initial commit and an installed,
authenticated Harness. Set your author inside the repository with
`git config user.name` and `git config user.email`, because the controller
ignores your global Git identity. Replace the example's Python test
command with verification appropriate to your project.

```sh
cd your-repository
machinist start "Reject unknown timezone names in parse_timezone with a ValueError" --test-cmd "uv run pytest"
```

Read the saved Spec and copy the exact Approval command printed by start:

```sh
machinist approve --task T1 --spec-sha <full-spec-commit-sha>
```

Approval continues implementation, verification, and independent Review. Use
status to find the report and candidate, inspect the report and diff, then
integrate the reviewed change:

```sh
machinist status T1
# Read the report and inspect the candidate diff before accepting it.
machinist integrate T1
```

Completion output explains the next activity and includes a
command using your Task or issue ID. Local integration reports completion;
publication is an optional follow-up with an explicit forge selection.

First start reuses applicable root settings, discovering an installed Harness
and verification command when those settings are absent. Its local
settings and Task records live under `.machinist/runs/local/`, excluded through
Git's local exclude file. It does not require an origin, labels, a daemon, forge
authentication, or hosted workflows. Existing `machinist.yaml` settings remain
available. Local orchestration can still use a cloud model; offline inference
requires a separately configured local provider.

Verification runs in an isolated committed checkout; dependency folders from
your working repository are not copied. Use a self-preparing command such as
`npm ci && npm test` or `uv run pytest`, with its lockfile committed. A failing
baseline stops before the Spec Harness, and `machinist status T1` shows the
Gate's error and log directory. Correct the Gate in
`.machinist/runs/local/config.yaml` or its dependency setup, then run
`machinist retry --task T1 --phase spec`. If the committed baseline itself
needs a fix, commit it and start a new Task. To try the loop on something
disposable first, copy [examples/first-task](examples/first-task/README.md).

Before your first paid run, `machinist rehearse` exercises the whole local
workflow with a fake Harness and no model cost. `machinist doctor --local` is an
optional readiness check using the same configuration resolution as first start or your
saved local settings. It checks Git, Harness probes, and verification command
availability without creating a Task or requiring a forge. Add `--json` for
structured output. Add `--run-gates` only to execute project commands in your
current checkout; this does not prove dependencies are ready in a fresh Workshop.
Plain `machinist doctor` runs these local checks when no `machinist.yaml`
exists and its GitHub setup checks otherwise.

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

GitHub issue intake, trusted workflow Approval, draft PRs, independent Review,
and the watcher remain available as an optional mode. Set it up once per
repository:

```sh
machinist onboard
# Review, stage, commit, and push the generated setup files, then:
machinist doctor --run-gates && machinist watch
```

The [GitHub setup guide](docs/getting-started.md#github-setup-and-automation)
covers which setup files to stage, local and CI Spec generation, SHA-bound
Approval, Review, and recovery. Managed workflows pin the installed controller
version, so run `machinist sync-workflows` after each upgrade.

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
| `machinist retry --task <Tn> --phase execute --fresh --harness <name> [--model <id>]` | Retry once with another installed Harness or model; the choice is not saved. |
| `machinist amend --task <Tn> --feedback <text>` | Turn feedback on a reviewed candidate into a new Spec that needs fresh Approval. |
| `machinist init [--yes]` | Create config, spec storage, labels, managed issue form, and workflows; asks setup questions in a terminal (`--yes` hands-free, `--no-input` skips without auto-enabling test command). |
| `machinist onboard [--setup-pr] [--yes]` | Run guided setup in place or deliver only managed setup files on a draft PR; `--yes` accepts defaults + detected test command. |
| `machinist rehearse [--harness]` | Exercise production local Phases, Git, verification, Review, and integration; paid Harness use is opt-in. |
| `machinist doctor [--run-gates]` | Run read-only setup and workflow-drift diagnostics; single health check that prints the exact fix for any `FAIL` (only run individual `--check` commands if doctor asks). Without `machinist.yaml`, plain `doctor` runs the local readiness checks. |
| `machinist doctor --local [--run-gates] [--json]` | Optional local readiness, without forge setup or saved state; Gate execution requires `--run-gates`. |
| `machinist update-check [--json] [--timeout <seconds>]` | Compare the installed release against PyPI, print how to upgrade, and report managed-workflow drift. |
| `machinist sync-workflows [--check]` | Write or verify config-derived workflows. |
| `machinist sync-labels --check\|--apply` | Verify or create the two configured lifecycle labels. |
| `machinist config validate\|show\|schema\|set` | Validate, inspect, export, or atomically update configuration; without `machinist.yaml`, `--path` defaults to the saved local settings. |
| `machinist task template --write\|--check` | Project or verify the sealed GitHub issue form. |
| `machinist task new --title <title> [--body-file <path>] [--dispatch]` | Create a structured GitHub issue; preserve drafts on failure and dispatch only after lint passes. |
| `machinist task lint <issue> [--json]` | Check objective, acceptance criteria, constraints, and verification readiness. |
| `machinist spec <issue> [--dry-run]` | Preview a Spec, or generate it and open its draft PR. |
| `machinist spec <issue> --revise` | Regenerate a successful Spec on its existing branch and PR. |
| `machinist spec <issue> --abandon [--reason <text>]` | Record rejection and close the open draft PR. |
| `machinist approve [--issue <issue>\|--pr <pr>]` | Request asynchronous workflow Approval for the current PR head; wait for trusted Evidence before Execute. |
| `machinist run <issue>` | Implement an approved Spec and run the configured Verification Gates. |
| `machinist review <issue>` | When legacy Review is enabled, independently review the exact implemented draft and mark it ready. |
| `machinist amend <issue> --feedback <text>` | Rework a ready PR from explicit feedback after fresh approval. |
| `machinist cancel <issue> [--reason <text>\|--clear]` | Cooperatively stop or block an issue's dispatch. |
| `machinist watch [--once] [--dry-run] [--max-tasks <n>]` | Preview or dispatch eligible tasks continuously or once. |
| `machinist queue pause\|resume\|defer\|allow\|show` | Persist operator controls over new watcher dispatches. |
| `machinist service install\|start\|restart\|stop\|status\|logs\|uninstall` | Manage the repository's macOS launchd watcher; destructive lifecycle actions refuse active Claims unless forced. |
| `machinist explain <issue> [--json]` | Show effective policy, resolved profiles, attempts, and the exact next action without secrets. |
| `machinist status [--local\|--all] [--json]` | With local configuration, default status and `--local` show local Tasks. Otherwise, default status shows the GitHub board and `--local` reads legacy Run Evidence. `--all` shows the registered GitHub portfolio. |
| `machinist status --watch [--interval <seconds>] [--json]` | Emit changed-only live pipeline snapshots until Ctrl-C. |
| `machinist runs [--issue <issue>] [--json]` | Read current, historical, orphaned, and corrupt local run records. |
| `machinist report [--source all\|legacy\|local] [--since 30d] [--json] [--otlp-endpoint <url>]` | Aggregate both history namespaces by default; local/all export requires an explicit endpoint. |
| `machinist retry <issue> [--phase spec\|execute\|review]` | Re-enable one failed Task Run. |
| `machinist retry <issue> --phase execute --run [--resume\|--fresh]` | Reuse a retained workspace or start a fresh Execute attempt; fresh is the default. |
| `machinist inspect <issue> [--offline] [--json]` | Show GitHub, workspace, and complete Task Run diagnostics. |
| `machinist repo add\|remove\|list` | Maintain the optional local repository registry. |
| `machinist clean [--issue <issue>\|--task <Tn>\|--all]` | List or remove retained Workshops for GitHub issues and local Tasks. |

## Upgrade

Upgrade an existing tool installation with `uv tool upgrade agentmachinist`.

`machinist update-check` compares the installed release against PyPI and
prints the upgrade command for how this copy was installed (`uv tool`, `pipx`,
`pip`, or a source checkout). `machinist doctor` reports the same result as a
diagnostic row. Set `MACHINIST_NO_UPDATE_CHECK=1` to suppress both probes on
offline or CI machines.

Upgrading the package is not always the whole upgrade. Managed workflows are
projected files: run `machinist sync-workflows`, review the generated changes,
and commit and merge them into the default branch for hosted workflows to use them.
`machinist watch` reports local drift at startup and
`machinist update-check` reports it alongside the release comparison, so you do
not have to run `doctor` to find out. The advisory never blocks a command and
never appears in `update-check --json`.

## Documentation

1. [Understand the workflow](docs/how-it-works.html): one diagram of your decisions
   and the controller's work. The [Approval policy](docs/approval-policy.md)
   explains what each Approval authorizes.
2. [Complete your first Task](docs/tldr.md): the short installation-to-integration
   guide. Prefer illustrated instructions? Use the
   [visual first-run guide](https://agentmachinist.vinny.dev/first-run-guide.html).

The [complete documentation index](docs/README.md) links every guide, reference,
architecture decision, and historical plan. For detailed settings, use the
[configuration and GitHub reference](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/getting-started.md).
Contributor and release information lives in [CONTRIBUTING.md](CONTRIBUTING.md)
and the [changelog](CHANGELOG.md).

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

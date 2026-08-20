# AgentMachinist

AgentMachinist is a local-first, issue-to-reviewed-PR build pipeline for solo
developers. It connects GitHub issues to Claude Code, OpenCode, Pi, or Codex,
with a human-approved specification between planning and implementation.

```text
issue + trigger label → spec commit → draft PR → SHA-bound approval
                    → implementation → test gate → ready PR → human merge
```

The controller—not the harness—owns commits, pushes, PR transitions, and task
records. AgentMachinist never merges.

Current release: [AgentMachinist 0.4.0 on PyPI](https://pypi.org/project/agentmachinist/0.4.0/).

## Install

```sh
uv tool install agentmachinist
```

You also need `git`, an authenticated [`gh`](https://cli.github.com), and one
supported harness executable (`claude`, `opencode`, `pi`, or `codex`).

## Start

```sh
cd your-repository
machinist init
# Answer the setup questions, review the generated files, then:
machinist doctor
machinist sync-workflows --check
git status --short
git add machinist.yaml .machinist/specs/.gitkeep .gitignore
git add -p .github/workflows
git diff --cached
git commit -m "chore: configure AgentMachinist"
git push
machinist watch
```

In a terminal, `machinist init` asks a short set of setup questions — dispatch
mode, managed workflows, harness, test gate, and notifications — each with a
one-line explanation and a safe default. Flags such as `--harness`,
`--test-cmd`, `--spec-source`, and `--notifications` pre-answer their
questions; `--no-input` (or a non-interactive shell) skips the questions and
uses the defaults plus test-command auto-detection.

Review the staged diff before committing. Managed workflows must be pushed
before GitHub comment or label approval can record SHA-bound evidence.
`machinist init` also adds `/.machinist/runs/` to `.gitignore`. If you manage
workflows yourself, `machinist init --no-workflows` records
`github.manage_workflows: false`; `doctor` then reports that its drift check was
intentionally skipped.

The default `github.spec_source: local` makes `watch` own spec generation.
Choose `github-actions` and run `machinist sync-workflows` if CI should own that
phase instead. Exactly one source is active, preventing duplicate spec runs.

Approval is bound to the exact PR head commit. Use either:

```sh
machinist approve --issue 57
# or: machinist approve --pr 18
# or post the SHA-bound comment shown in the Spec PR body:
# /machinist-execute <full-spec-commit-sha>
```

The positional target remains available when that number identifies only one
Task; use `--issue` or `--pr` when GitHub numbers overlap.

Editing the spec after approval makes that approval stale and blocks execution
until the new head is approved.

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
| `machinist init` | Create config, spec storage, labels, and managed workflows; asks setup questions in a terminal (`--no-input` skips them). |
| `machinist doctor` | Run read-only setup and workflow-drift diagnostics. |
| `machinist sync-workflows [--check]` | Write or verify config-derived workflows. |
| `machinist config validate\|show\|schema\|set` | Validate, inspect, export, or atomically update configuration. |
| `machinist spec <issue> [--dry-run]` | Preview a Spec, or generate it and open its draft PR. |
| `machinist spec <issue> --revise` | Regenerate a successful Spec on its existing branch and PR. |
| `machinist spec <issue> --abandon [--reason <text>]` | Record rejection and close the open draft PR. |
| `machinist approve [--issue <issue>\|--pr <pr>]` | Bind approval to the current PR head without number ambiguity. |
| `machinist run <issue>` | Implement an approved spec and run the test gate. |
| `machinist amend <issue> --feedback <text>` | Rework a ready PR from explicit feedback after fresh approval. |
| `machinist cancel <issue> [--reason <text>\|--clear]` | Cooperatively stop or block an issue's dispatch. |
| `machinist watch [--once] [--dry-run] [--max-tasks <n>]` | Preview or dispatch eligible tasks continuously or once. |
| `machinist queue pause\|resume\|defer\|allow\|show` | Persist operator controls over new watcher dispatches. |
| `machinist service install\|start\|restart\|stop\|status\|logs\|uninstall` | Manage the repository's macOS launchd watcher. |
| `machinist status [--local\|--all] [--json]` | Show GitHub state, local Task Runs, or a registered portfolio. |
| `machinist runs [--issue <issue>] [--json]` | Read current, historical, orphaned, and corrupt local run records. |
| `machinist retry <issue> [--phase spec\|execute]` | Re-enable one failed Task Run. |
| `machinist retry <issue> --phase execute --run [--resume\|--fresh]` | Reuse a retained workspace or start a fresh Execute attempt; fresh is the default. |
| `machinist inspect <issue> [--offline] [--json]` | Show GitHub, workspace, and complete Task Run diagnostics. |
| `machinist repo add\|remove\|list` | Maintain the optional local repository registry. |
| `machinist clean [--issue <issue>\|--all]` | List or remove retained workspaces. |

## Documentation

- [Getting started](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/getting-started.md)
- [Visual first-run field guide](https://vscarpenter.github.io/AgentMachinist/first-run-guide.html)
- [Architecture and lifecycle](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/architecture.md)
- [Operator runbook](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/operator-runbook.md)
- [Trust model](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/trust-model.md)
- [Harness support](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/harnesses.md)
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
distributions, smoke-tests the wheel, and records SHA-256 hashes. A minimal job
then publishes those verified artifacts. Only after publication do separate
jobs attach the distributions and checksum file to the GitHub Release and
verify that the exact version is visible and installable from PyPI.

## License

MIT — see [LICENSE](https://github.com/vscarpenter/AgentMachinist/blob/main/LICENSE).

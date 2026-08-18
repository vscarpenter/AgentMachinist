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

Current release: [AgentMachinist 0.3.0 on PyPI](https://pypi.org/project/agentmachinist/0.3.0/).

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
# Set tests.command in machinist.yaml, then:
machinist doctor
machinist watch
```

The default `github.spec_source: local` makes `watch` own spec generation.
Choose `github-actions` and run `machinist sync-workflows` if CI should own that
phase instead. Exactly one source is active, preventing duplicate spec runs.

Approval is bound to the exact PR head commit. Use either:

```sh
machinist approve 57
# or post the exact PR comment: /machinist-execute
```

Editing the spec after approval makes that approval stale and blocks execution
until the new head is approved.

## Commands

| Command | Purpose |
| --- | --- |
| `machinist init` | Create config, spec storage, labels, and managed workflows. |
| `machinist doctor` | Run read-only setup and workflow-drift diagnostics. |
| `machinist sync-workflows [--check]` | Write or verify config-derived workflows. |
| `machinist spec <issue>` | Generate a spec and open its draft PR. |
| `machinist approve <pr>` | Bind approval to the current PR head. |
| `machinist run <issue>` | Implement an approved spec and run the test gate. |
| `machinist watch [--once]` | Dispatch eligible tasks continuously or once. |
| `machinist status` | Show issue/PR lifecycle states. |
| `machinist retry <issue> [--phase spec\|execute]` | Re-enable one failed Task Run. |

## Documentation

- [Getting started](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/getting-started.md)
- [Visual first-run field guide](https://vscarpenter.github.io/AgentMachinist/first-run-guide.html)
- [Architecture and lifecycle](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/architecture.md)
- [Operator runbook](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/operator-runbook.md)
- [Trust model](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/trust-model.md)
- [Harness support](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/harnesses.md)
- [Visual handbook](https://github.com/vscarpenter/AgentMachinist/blob/main/docs/onboarding.html)
- [Contributing](https://github.com/vscarpenter/AgentMachinist/blob/main/CONTRIBUTING.md)
- [Changelog](https://github.com/vscarpenter/AgentMachinist/blob/main/CHANGELOG.md)

The trust model is deliberately narrower than “the agent cannot use git.”
Harness flags, credential reduction, repository postconditions, and push leases
reduce risk, but local harnesses still execute with the operating-system access
of the user who launched them. Read the trust model before unattended use.

## Releasing

Releases use PyPI Trusted Publishing. Bump `pyproject.toml`, update the
changelog, and publish a GitHub Release tagged `v<version>`. The release job
requires tag/version equality, runs the suite, builds both distributions,
installs and smoke-tests the wheel, and publishes last.

## License

MIT — see [LICENSE](https://github.com/vscarpenter/AgentMachinist/blob/main/LICENSE).

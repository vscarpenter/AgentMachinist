# AgentMachinist

Local-first agentic build & CI/CD for solo developers. AgentMachinist bridges
GitHub issues to local coding harnesses — Claude Code, OpenCode, PI, or
Codex — through a three-phase, human-in-the-loop pipeline that runs on your Mac.

```
 GitHub issue (label: agent-task)
        │
        ▼
 Phase 1 · SPEC        harness writes .machinist/specs/issue-<n>-spec.md
        │              → branch agent/issue-<n> → draft PR (Closes #<n>)
        ▼
 Phase 2 · APPROVE     you review the spec; apply label machinist:approved
        │              (or comment /machinist-execute on the PR)
        ▼
 Phase 3 · EXECUTE     local daemon implements the spec in an isolated
                       worktree, runs your tests, pushes to the PR branch,
                       and marks it ready for review
```

The agent never merges anything. You approve the spec before code is written,
and you review the PR before it lands.

## Install

```sh
uv tool install agentmachinist        # or: uv tool install git+https://github.com/vscarpenter/AgentMachinist
```

Prerequisites: [`gh`](https://cli.github.com) (authenticated), `git`, and at
least one coding harness CLI (`claude`, `opencode`, `pi`, or `codex`).

## Quickstart

In the repository you want agents to work on:

```sh
machinist init          # writes machinist.yaml, .machinist/, and GitHub workflows
machinist spec 42       # Phase 1 for issue #42 (or let `watch` pick it up)
machinist watch         # daemon: polls for labeled issues and approved PRs
machinist run 42        # Phase 3 for an approved spec
```

## Configuration (`machinist.yaml`)

```yaml
version: 1
harness:
  name: claude-code            # claude-code | opencode | pi | codex
  command: null                # optional executable override
  timeout_minutes: 30          # Phase 3 implementation budget
  spec_timeout_minutes: 10     # Phase 1 spec budget
github:
  repo: null                   # "owner/repo"; null = derived from origin
  labels:
    trigger: agent-task
    approved: "machinist:approved"
  poll_interval_seconds: 60
workspace:
  root: ~/.machinist/workspaces
  strategy: worktree           # worktree | clone
  cleanup: on_success          # always | on_success | never
  branch_prefix: agent/
tests:
  command: null                # e.g. "pytest -q"; null skips the test gate
```

Unknown keys are rejected — typos fail loudly instead of being ignored.

## How approval works

The single source of truth is the `machinist:approved` label on the draft PR.
Apply it by hand, or comment `/machinist-execute` on the PR — the bundled
`machinist-approve.yml` workflow converts that comment into the label (only
for repo owners, members, and collaborators). Draft → Ready for Review is
reserved as the *agent's* signal that implementation is complete.

## Spec generation: local or CI

Both paths run the same `machinist spec <n>` command:

- **Local (default):** `machinist watch` sees the `agent-task` label and
  generates the spec on your machine using your existing harness login.
- **CI:** the bundled `machinist-spec.yml` workflow runs it in GitHub Actions
  when an issue is labeled — works while your Mac sleeps, but requires an
  `ANTHROPIC_API_KEY` repository secret.

## Status

v0.1 (M1): `machinist init` and `machinist spec <n>` work end-to-end —
issue → harness-written spec → `agent/issue-<n>` branch → draft PR, in an
isolated worktree that never touches your checkout. Phase 3 (`run`) and the
`watch` daemon are landing next — see `docs/superpowers/specs/` for the design.

## License

MIT

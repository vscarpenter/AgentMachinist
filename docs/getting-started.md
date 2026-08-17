# Getting Started with AgentMachinist

AgentMachinist turns a GitHub issue into a reviewed implementation spec, written by the coding agent you already use. You stay in control at every step: no spec becomes code without your approval, and nothing merges without your review. This guide takes you from a fresh install to your first agent-written spec PR.

## What is AgentMachinist?

AgentMachinist is a local-first pipeline for solo developers. It bridges GitHub issues to local coding harnesses (Claude Code, OpenCode, PI, or Codex) through three phases:

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

Two guarantees hold throughout. The agent never merges anything, and it never touches your working checkout. You approve the spec before the agent writes any code, and you review the PR before it lands.

All three phases ship in v0.1 — this guide's own repository ran the full loop on itself as the pipeline's first task. The [What's next](#whats-next-v01-limits) section maps the remaining edges.

## Before you begin

You need four tools installed and working:

| Tool | Why | Verify with |
| --- | --- | --- |
| `git` | Branches, worktrees, commits, and pushes. | `git --version` |
| [`gh`](https://cli.github.com) | Reads issues, opens draft PRs, and handles all GitHub auth. | `gh auth status` |
| [`uv`](https://docs.astral.sh/uv/) | Installs and updates the CLI. | `uv --version` |
| A harness CLI | Writes the specs. One of `claude`, `opencode`, `pi`, or `codex`. | `claude --version` |

You also need a GitHub repository you can push to. AgentMachinist never handles tokens itself: locally `gh` uses your existing login, and in GitHub Actions it uses the runner's `GITHUB_TOKEN`.

## Install

The package is not on PyPI yet, so install it straight from GitHub:

```sh
uv tool install git+https://github.com/vscarpenter/AgentMachinist
```

Confirm the CLI landed on your PATH:

```sh
machinist --version
```

## Set up your repository

Run the one-time setup from the root of the repository you want agents working on:

```sh
machinist init
```

It writes these artifacts:

- `machinist.yaml`: the configuration file. The defaults work out of the box; the [Configuration reference](#configuration-reference) explains every key.
- `.machinist/specs/`: where generated specs land. A `.gitkeep` file keeps the empty directory in git.
- `.github/workflows/machinist-spec.yml`: the optional CI path for spec generation, covered [below](#spec-generation-local-or-ci).
- `.github/workflows/machinist-approve.yml`: turns a `/machinist-execute` PR comment into the approval label.

It finishes by printing your next steps:

```
Next steps:
  1. Review machinist.yaml (harness, labels, test command).
  2. Commit the new files and push.
  3. For CI spec generation, add an ANTHROPIC_API_KEY repository secret.
  4. Label an issue 'agent-task' to start the pipeline.
```

Two flags adjust the setup. `machinist init --no-workflows` skips the `.github/workflows/` files if you want the local path only. And `machinist init` refuses to overwrite an existing `machinist.yaml`; pass `--force` when you mean it.

The `ANTHROPIC_API_KEY` secret matters only for CI spec generation. The local path reuses whatever login your harness CLI already has, so you can skip step 3 for now.

Commit the new files and push before moving on. The two workflows only take effect once they reach your default branch.

## Your first agent task

**1. Write an issue worth automating.** The issue title and body become the harness prompt, so a crisp problem statement with acceptance criteria pays off directly.

**2. Label it `agent-task`.** The label triggers the CI workflow today and the watch daemon in a coming milestone. When you run Phase 1 by hand, the label is good bookkeeping rather than a requirement.

**3. Run Phase 1** with your issue number:

```sh
machinist spec 7
```

Here is what happens, step by step:

1. AgentMachinist reads the issue's title and body through `gh`.
2. It provisions an isolated worktree at `~/.machinist/workspaces/<repo>-issue-7` on a fresh branch, `agent/issue-7`, cut from your default branch. Your own checkout stays untouched.
3. The harness runs inside that worktree in read-only print mode. It can explore your code for context, but it cannot edit anything; it prints the spec to stdout. It gets `spec_timeout_minutes` (default 10) to finish.
4. AgentMachinist writes the output to `.machinist/specs/issue-7-spec.md`, commits, and pushes the branch.
5. It ensures the `machinist:approved` label exists in your repository, then opens a draft PR titled `Spec: <issue title> (#7)`. The PR body carries `Closes #7`, so merging the eventual implementation closes the issue.
6. On success, it removes the worktree again (the default `cleanup: on_success` policy).

The command finishes by printing the PR and your next move:

```
Draft PR #8: https://github.com/you/your-repo/pull/8
Review the spec, then approve with the 'machinist:approved' label or a /machinist-execute comment.
```

## Review and approve

Open the draft PR and read the spec under **Files changed**. It is the contract for the implementation, so review it like one: check the interpretation of the issue, the proposed approach, and the testing plan.

When it looks right, approve it either way:

- apply the `machinist:approved` label to the PR, or
- comment `/machinist-execute` on the PR. The bundled `machinist-approve.yml` workflow converts that comment into the label. It only honors comments from the repository owner, organization members, and collaborators, and it ignores everyone else.

If the spec misses the mark, treat `agent/issue-7` like any PR branch and push edits to the spec file. Or close the PR, sharpen the issue, and rerun `machinist spec 7`.

One v0.1 honesty note: the label is the durable approval record, but nothing consumes it yet. Phase 3, which turns approved specs into code, is the next milestone.

## Spec generation: local or CI

Both paths run the same `machinist spec <n>` command; they only differ in where it runs.

**Local (the default).** You run the command by hand, as you just did. The harness uses your existing login, so no API key is involved. In a later milestone, `machinist watch` automates this by polling for the `agent-task` label.

**CI.** The bundled `machinist-spec.yml` workflow runs the same command in GitHub Actions whenever an issue gets the `agent-task` label. It works while your Mac sleeps. It needs an `ANTHROPIC_API_KEY` repository secret, and it installs Claude Code on the runner regardless of your local harness choice.

Prefer local only? Delete `.github/workflows/machinist-spec.yml`, or run `machinist init --no-workflows` from the start.

## Configuration reference

`machinist init` writes this file with every default spelled out. The loader rejects unknown keys, so a typo fails loudly with a readable error instead of hiding.

```yaml
version: 1

harness:
  name: claude-code            # claude-code | opencode | pi | codex
  command: null                # optional: override the harness executable path
  timeout_minutes: 30          # Phase 3 implementation budget (1-240)
  spec_timeout_minutes: 10     # Phase 1 spec-writing budget (1-60)

github:
  repo: null                   # "owner/repo"; null = gh derives it from origin
  labels:
    trigger: agent-task        # issues with this label enter the pipeline
    approved: "machinist:approved"   # PRs with this label get implemented
  poll_interval_seconds: 60    # how often the watch daemon polls (min 10)

workspace:
  root: ~/.machinist/workspaces
  strategy: worktree           # worktree (shared object store) | clone
  cleanup: on_success          # always | on_success | never
  branch_prefix: agent/        # spec branches become <prefix>issue-<number>

tests:
  command: null                # e.g. "pytest -q" or "npm test"; null skips the gate
```

### `harness`

- `name` picks the adapter: `claude-code` (default), `opencode`, `pi`, or `codex`. More in [Choosing a harness](#choosing-a-harness).
- `command` overrides the harness executable. `null` uses the adapter's default command; set a full path when the CLI lives outside your PATH.
- `timeout_minutes` caps a Phase 3 implementation run. Default 30, accepted range 1 to 240.
- `spec_timeout_minutes` caps a Phase 1 spec run. Default 10, accepted range 1 to 60. A timeout kills the harness process and fails the phase.

### `github`

- `repo` names the target repository and must look exactly like `owner/repo`. The default `null` lets `gh` derive it from your `origin` remote, which is usually what you want.
- `labels.trigger` (default `agent-task`) marks issues that enter the pipeline. If you rename it, update the matching label name in `.github/workflows/machinist-spec.yml` as well; the workflow checks it by name.
- `labels.approved` (default `machinist:approved`) is the approval signal on draft PRs. Phase 1 creates the label in your repository automatically.
- `poll_interval_seconds` (default 60, minimum 10) sets how often the coming watch daemon polls GitHub.

### `workspace`

- `root` (default `~/.machinist/workspaces`) holds one checkout per task.
- `strategy` picks the isolation style. `worktree` (default) shares your repository's object store, which keeps it fast and cheap. `clone` makes a fully independent copy pointing at the same remote.
- `cleanup` decides what happens to a finished checkout. `on_success` (default) removes it after a clean run but keeps failures around for debugging. `always` removes it no matter what. `never` keeps everything.
- `branch_prefix` (default `agent/`) shapes branch names, so issue 7 becomes `agent/issue-7`.

### `tests`

- `command` is the gate Phase 3 will run before pushing an implementation, for example `pytest -q` or `npm test`. `null` skips the gate. Phase 1 never runs tests.

Snippets in this guide are full-file fragments. Any of them pastes straight into `machinist.yaml`, and every field you omit keeps its default:

```yaml
tests:
  command: pytest -q
```

## Choosing a harness

The harness is the coding agent that reads your code and writes the spec. Pick the one you already use and are logged into; AgentMachinist inherits that login for local runs.

| `harness.name` | Executable | Headless invocation |
| --- | --- | --- |
| `claude-code` | `claude` | `claude -p <prompt>` (the default harness) |
| `opencode` | `opencode` | `opencode run <prompt>` |
| `pi` | `pi` | `pi -p <prompt>` |
| `codex` | `codex` | `codex exec <prompt>` |

Switch by editing one line:

```yaml
harness:
  name: codex
```

If the executable lives somewhere unusual, point `command` at it:

```yaml
harness:
  name: claude-code
  command: /opt/homebrew/bin/claude
```

One caveat: the CI workflow installs Claude Code specifically. If you pick another harness and still want CI spec generation, edit `.github/workflows/machinist-spec.yml` to install yours instead.

## Troubleshooting

Every expected failure prints as a single-line error with a nonzero exit, never a traceback. Here is what each one means.

| You see | What happened | Fix |
| --- | --- | --- |
| `machinist.yaml not found. Run 'machinist init' first.` | You ran a command outside an initialized repository. | Run `machinist init` from the repository root. |
| `gh CLI not found. Install it (https://cli.github.com) and run 'gh auth login'.` | The GitHub CLI is missing from your PATH. | Install `gh`, then authenticate with `gh auth login`. |
| `harness executable 'claude' not found; install it or set harness.command` | The configured harness CLI is not installed or not on your PATH. | Install it, or set `harness.command` to its full path. |
| `claude-code timed out after 10 minutes` | The spec run blew past `spec_timeout_minutes`. | Raise the timeout (up to 60), or split the issue into a smaller ask. |
| `claude-code returned an empty spec for issue #7` | The harness exited cleanly but printed nothing, usually a login or model-access problem. | Run the harness CLI interactively once and confirm it responds. |
| `workspace <path> already exists; remove it (or 'git worktree remove' it) and retry` | An earlier run left its checkout behind, usually a failed run under the default `cleanup: on_success`. | Run `git worktree remove --force <path>` from your repository, or delete the directory if you use the `clone` strategy. |

Failed runs keep their workspace on purpose so you can inspect exactly what the harness saw. That same debris causes the last error above, so clean up when you are done looking.

## What's next (v0.1 limits)

v0.1 covers milestones M0 through M3 of the [design doc](superpowers/specs/2026-08-16-agentmachinist-design.md). Working today, end to end:

- `machinist init`: repository setup.
- `machinist spec <n>`: issue to draft spec PR.
- `machinist run <n>`: Phase 3 — implements an approved spec in an isolated workspace, runs your `tests.command` gate, pushes, and marks the PR ready for review. It refuses to start unless the PR carries the approval label.
- `machinist watch`: the daemon that polls for `agent-task` issues and approved PRs and dispatches the phases automatically. Use `machinist watch --once` for a single pass (handy under cron). An issue that fails is not retried for the daemon's lifetime — fix it, restart, and it dispatches again.
- `machinist status`: pipeline state at a glance — `awaiting spec`, `awaiting approval`, `approved`, or `in review`.

Known edges rather than missing commands: spec quality depends on your harness and issue quality; the `clone` workspace strategy is less exercised than `worktree`; and the CI spec workflow needs an `ANTHROPIC_API_KEY` secret you may prefer not to create — the local daemon covers that path.

That is the whole loop. Write a sharp issue, read the spec like you mean it, and keep your hand on the merge button.

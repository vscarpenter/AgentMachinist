# AgentMachinist TL;DR

Quick-reference instructions for driving the pipeline on your machine.
Label names come from `machinist.yaml` (`github.labels`); the defaults are
`agent-task` (trigger) and `machinist:approved` (approval).

One setting changes the whole flow: `github.spec_source`.

- `local` — your `machinist watch` daemon owns Phase 1 (spec writing).
- `github-actions` — CI owns Phase 1.

Either way, Execute and Review always run on your machine via `watch`/`run`;
the setting only moves spec generation. Approval is always SHA-bound: the
label alone is never enough — the managed approve workflow must also record
a trusted comment marker matching the exact PR head.

## TL;DR — Local flow (`spec_source: local`)

One-time setup (per repo):

```sh
cd your-repo
machinist onboard                     # answer questions → creates machinist.yaml, labels, workflows
machinist doctor --run-gates          # preflight: git, gh, harness, test gate
machinist sync-labels --check         # verify labels exist on GitHub
machinist sync-workflows --check      # verify workflows match config
git add machinist.yaml .github/ && git commit -m "chore: configure AgentMachinist" && git push
machinist watch                       # start the daemon (or: machinist service install/start)
```

Each task:

```sh
# 1. Create the issue + apply the trigger label
machinist task new --title "Fix login redirect"   # or: gh issue create + gh issue edit <n> --add-label agent-task

# 2. Daemon picks it up → harness writes spec → draft PR appears
machinist status                                  # check progress

# 3. Review the spec PR on GitHub, then approve (binds to exact SHA)
machinist approve --issue <n>

# 4. Daemon implements it, runs your tests, does read-only review → PR goes ready
machinist inspect <n>                             # see everything in one pass

# 5. You review + merge on GitHub. Done.
```

Useful along the way: `machinist status -v`, `machinist retry <n>
--phase spec|execute|review` after any failure, `machinist spec <n> --revise`
to redo a bad spec.

## TL;DR — GitHub flow (`spec_source: github-actions`)

One-time setup (per repo):

```sh
cd your-repo
machinist init --spec-source github-actions      # or onboard; needs an ANTHROPIC_API_KEY repo secret
machinist sync-labels --check
machinist sync-workflows                         # writes .github/workflows/machinist-*.yml
git add . && git commit -m "chore: configure AgentMachinist" && git push
```

Plus: add the harness API key secret
(`gh secret set ANTHROPIC_API_KEY`).

Each task:

```sh
# 1. Create the issue on GitHub (use the managed Task form) and add the label
gh issue create --title "Fix login redirect" --body "..." --label agent-task
# (or click "Task" template in the GitHub UI and add the agent-task label)

# 2. CI sees the label → runs the Spec phase → draft PR appears on GitHub
#    (nothing runs on your machine yet)

# 3. Review the spec PR, then approve — either:
gh pr comment <pr> --body "/machinist-execute <full-spec-commit-sha>"
#    (the SHA is shown in the PR body) — or from your machine:
machinist approve --issue <n>

# 4. The approve workflow verifies your write access + the exact SHA,
#    then records the evidence and label.

# 5. Now execute locally (CI never implements):
machinist run <n>          # or let `machinist watch` dispatch it automatically

# 6. Implementation + test gate + independent review run → PR goes ready
# 7. You merge on GitHub. Done.
```

## The mental model in one line each

- Local: `label issue → watch writes spec → you approve → watch implements → you merge`
- GitHub: `label issue → CI writes spec → you approve (comment or CLI) → run/watch implements locally → you merge`

## Gotchas worth remembering

- **Never edit the managed workflows by hand** — they are projected from
  `machinist.yaml`, and the next `sync-workflows` silently replaces your
  edits. Drift shows up in `doctor`, `watch` startup, and `update-check`.
- **Approval goes stale automatically** — if the spec branch moves after you
  approve, the old approval no longer matches the PR head and execution is
  blocked until you re-approve. That is the SHA-binding doing its job, not a
  bug.
- **A ready PR is the finish line** — AgentMachinist never merges; the human
  gate at the end is deliberate.

For the full walkthrough, see [getting-started.md](getting-started.md).
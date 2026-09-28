# Review of next-steps-spec.md (2026-09-28)

## Verdict

`next-steps-spec.md` describes a different product. It specifies a TypeScript
monorepo that calls model providers directly, hosts MCP servers, and restarts
Kubernetes deployments. AgentMachinist is a Python controller that runs an
external Harness around an exact, human-approved Spec commit. The controller
never calls a model API (no `anthropic`, `openai`, `httpx`, or `requests`
imports under `src/machinist`) and has no MCP code.

The header "Status: Approved for Implementation" is not accurate. Nobody
approved it, and it contradicts ADR 0001 (no Evidence as trace events, no OTel
SDK) and ADR 0003 (local workflow, human integration). Do not hand it to an
agent as written: it would rewrite the package in another language and rename
the published `machinist` CLI.

Three kernels survive. Everything else either already exists or belongs to the
Harness.

## Per-feature verdicts

| # | Feature | Verdict | Why |
|---|---------|---------|-----|
| 1 | Zero-config `init` | Keep kernel | `start`, `init`, and `doctor --local` already detect the Harness and test Gate. The open work is the first-run friction in `tasks/onboarding-review-2026-09-27.md`. Probing API keys and writing `.env.local` would make the controller hold secrets, which the credential scrubbing in `harness/base.py` exists to avoid. |
| 2 | MCP client and registry | Kill | The Harness owns tools, and Claude Code, Codex, and Goose already speak MCP. A controller-side MCP host widens the credential surface `docs/trust-model.md` works to reduce. Per-Task MCP config can already pass through `harness.extra_args`. |
| 3 | Debugger UI with WebSocket replay | Kill | There are no in-process prompts, completions, or token budgets to show. `status T1 -v --json`, `inspect`, `explain`, and the Task Run JSON cover the controller's view. |
| 4 | FSM with HITL gates | Already shipped | `transitions.py` is the state machine, and Approval binds an exact Spec SHA (invariant 2). Per-state `allowed_tools` is a Harness concern. "Reject with reason" maps to `amend --task T1 --feedback`. |
| 5 | Tool-call schema validation | Kill | The controller never sees tool calls. The analogues ship already: fail-closed Review parsing (`phases/review.py:371`) and bounded repair (`repair.py`). |
| 6 | OTel GenAI tracing | Defer | ADR 0001 rejects Evidence as trace events and an SDK dependency. No `gen_ai` spans exist to emit. Per-Phase duration spans from allowlisted fields would need ADR 0005 and a user asking for them. |
| 7 | Prompt manifests with hot reload | Kill hot reload | `instructions:` already adds per-Phase repository prompt text, hashed as `instructions_sha256` (`config.py:568`). Execute resume refuses a changed digest (`phases/local.py:409`, `phases/execute.py:156`). Hot reload would change approved behavior with no record. |
| 8 | Eval suite | Reframe | Trajectory and token assertions need visibility into the model loop. The useful kernel is a small regression corpus of fixed Tasks, compared by first-pass Execute outcome across Harnesses and models. |
| 9 | Model cascading on 429 | Reframe | The controller sees no HTTP status. An automatic switch would violate invariant 7 (explicit retry only). The safe kernel is an explicit Harness choice on `retry`. |
| 10 | Terraform and Kubernetes modules | Kill | These need a webhook server, pager integration, and production credentials. That is outside a local workflow for solo developers and small teams. |

## Errors in the spec worth knowing

- The model names are stale (`claude-3-7-sonnet`, `gpt-4o`).
- MCP replaced the HTTP+SSE transport with Streamable HTTP in the 2025-03-26 revision.
- `role: "tool_error"` is not a role in either major API. Anthropic returns
  `tool_result` with `is_error: true`, and MCP uses `isError`.
- Current GenAI semantic conventions renamed `gen_ai.system` to
  `gen_ai.provider.name`, and `generate_tool_call` is not a defined operation.
- The "cryptographic token" has no issuer, key, expiry, or binding to the
  proposed call. That is weaker than exact-SHA Approval.
- The 60-second quickstart goal names no hardware or network baseline, and it
  also requires an interactive key prompt.
- Falling back to a local model after repeated schema failures sends the
  hardest cases to the weakest model, including for mutating actions.
- `reject`, `escalate_to_pager`, and `workflows/k8s_triage.yaml` are referenced
  but never defined.

## Plan

Order follows value per unit of risk. Each item is Standard tier unless noted.

### 1. Close the onboarding review (S)

`tasks/onboarding-review-2026-09-27.md` lists seven contradictions and five
simplifications for the first-Task path. This is the real version of the
60-second goal, and it is already scoped. Add one measurable target: count the
commands from `pip install` to a saved Spec in `docs/tldr.md`, and have
`tests/test_docs.py` fail if it grows.

- Touches: `docs/tldr.md`, `README.md`, `cli.py` help text, `tests/test_docs.py`.
- Invariants at risk: none.
- First failing test: a doc test asserting the TLDR command count stays at or
  below the agreed number.
- Blocked on the four "Decisions for Vinny" in that review.

### 2. Explicit Harness choice on retry (M, needs `/qspec`)

`retry --task T1 --phase execute --run --harness <name> [--model <id>]` reruns
a failed Execute with a different installed Harness. This is the honest version
of Feature 9: a human decides, the choice is recorded, and nothing switches on
its own.

- Touches: `cli.py` (retry option), `dispatch.py` (Harness override into Task
  Run construction), `evidence.py` (record the Harness and model used),
  `docs/local-workflow.md`, `docs/operator-runbook.md`, `CHANGELOG.md`.
- Invariants at risk: 5 (the override must flow through `TaskDispatcher`),
  7 (only on explicit retry, never `--resume`, since a resumed run must keep its
  original Harness), and 9 (Review must still run against the new candidate).
- Assumption to verify first: local Approval binds the Spec SHA and Task, not
  the Harness. If Approval does bind the Harness, stop and re-plan.
- First failing test: an Execute retry with `--harness codex` builds its Task
  Run with the Codex adapter and records `harness: codex` in Evidence, while
  `--resume` with `--harness` fails with a clear error.

### 3. Cross-Harness regression corpus (M)

A small set of disposable Tasks built on `examples/first-task`, each with a
fixed objective and known Gates. A script runs each Task through the normal
pipeline with a chosen Harness and model, then reads the results through
`report --json`. This answers "did the prompt change or the new model make
first-pass Execute worse?" with the Evidence that already exists.

- Start as `scripts/bench.sh` plus fixtures, not a CLI command. Promote it only
  if it earns regular use.
- Touches: `examples/`, `scripts/`, no controller code.
- Invariants at risk: none. It drives the public CLI.
- First check: the script fails with a nonzero exit when any fixture's
  first-pass Execute outcome regresses against a committed baseline.
- Caveat: `reporting.py` marks usage as unknown when a Harness does not report
  it, so token comparisons will be sparse. Compare pass rate and duration first.

### Not planned

MCP, the debugger UI, schema interception, automatic cascading, prompt hot
reload, the DevOps modules, the TypeScript rewrite, and the CLI rename. Trace
spans wait for a concrete user request and an ADR.

## Housekeeping

`next-steps-spec.md` is untracked at the repository root. Either delete it or
change its status line to "Rejected; see tasks/next-steps-review.md" so no
agent treats it as an approved Spec.

# Harness support matrix

AgentMachinist supports four local adapters. “Spec control” describes the
adapter arguments; all adapters also face the controller's dirty-tree check.

| Config value | Executable | Spec and Review control | Implementation control | Managed Spec CI secret |
| --- | --- | --- | --- | --- |
| `claude-code` | `claude` | Plan permission mode, Read/Grep/Glob tools, no session persistence | Edit mode with gate commands allowlisted and no session persistence; prompt plus Git postconditions | `ANTHROPIC_API_KEY` |
| `codex` | `codex` | Read-only sandbox and ephemeral session | Workspace-write sandbox, approval prompts disabled, ephemeral session; prompt plus Git postconditions | `OPENAI_API_KEY` |
| `pi` | `pi` | Read/grep/find/ls allowlist; extensions, skills, prompt templates, and sessions disabled | Normal print-mode tools with session persistence disabled; prompt plus Git postconditions | `GEMINI_API_KEY` |
| `opencode` | `opencode` | Pure plan agent; treated as advisory | Normal run agent; prompt plus Git postconditions | `ANTHROPIC_API_KEY` by default |

Review is a separate durable Task Run even when it inherits the same adapter.
It receives the approved Spec, diff, verification evidence, and issue metadata,
and runs under the adapter's read-only Review argv. Its version-1 findings are
advisory; invalid output or a changed head leaves the pull request draft.

## Verification feedback loop

When gates are configured and `verification.harness_may_run_gates` is true
(the default), the implementation prompt lists each gate command and asks the
harness to run required gates and iterate until they pass before finishing.
`codex`, `pi`, and `opencode` execute modes already permit command execution,
so only the prompt changes for them. `claude-code`'s headless edit mode
denies commands, so the adapter additionally allowlists exactly the
configured gate commands (`--allowedTools "Bash(<command>)"
"Bash(<command>:*)"`). The controller's own gate run afterwards stays
authoritative.

## Authentication

Local runs use the harness's existing provider authentication. `doctor` checks
the installed version, parses both configured Phase invocations, and uses each
CLI's read-only authentication probe. That confirms configured credentials,
not subscription quotas or access to every possible model.

Current authentication entry points are:

| Harness | Check | Sign in or configure |
| --- | --- | --- |
| Claude Code | `claude auth status --json` | `claude auth login` |
| Codex | `codex login status` | `codex login` |
| OpenCode | `opencode auth list --pure` | `opencode auth login` |
| Pi | `pi auth check --model <model> --json --no-refresh` (or the default Google provider when no model is set) | Configure credentials for the selected provider or model, then rerun the check. |

These CLIs evolve independently. Confirm the command with the installed
harness's `--help` output when upgrading.

The GitHub Actions Spec template installs the selected adapter at the pinned
version declared in its descriptor and binds only the descriptor's secret.
`github.spec_secret_env` may change the repository secret name without storing
its value. A third-party adapter without `ci_spec` metadata is local-only and
workflow projection fails with the recovery choice instead of guessing.

## Credential environment

Harness subprocesses retain provider variables such as `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`. AgentMachinist removes its common GitHub token, Git askpass,
and SSH-agent variables and disables terminal credential prompting. This is
credential reduction, not credential isolation; see the
[trust model](trust-model.md).

## Model and additional arguments

`harness.model` passes one model selection to the adapter. `harness.extra_args`
is an advanced option applied to Spec, Execute, and Review. AgentMachinist rejects
adapter-owned sandbox, permission, model, session, and tool flags, including
duplicate forms that could override its controls. Other additional arguments
are appended to the adapter command and may change behavior as harness CLIs
evolve, so keep `extra_args` empty unless you have reviewed the final command
and updated your threat assessment.

## Third-party adapter contract

Plugins are trusted local Python code. A distribution registers exactly one
`Harness` subclass per entry point:

```toml
[project.entry-points."agentmachinist.harnesses.v1"]
example = "example_harness:ExampleHarness"
```

The entry-point name and class `name` must match a lowercase 1–64 character
identifier. Built-in names are reserved. `HarnessDescriptor` declares contract
version 1, display name, HTTPS documentation, supported phases, whether token
usage is structured, and optional `HarnessCIProfile` install argv plus secret
name. Discovery isolates load failures; one broken plugin cannot hide healthy
adapters, and the unknown-adapter error lists both installed choices and failed
entry points.

Adapters splice `self._passthrough_argv()` (the operator's `harness.model` and
`harness.extra_args`) into both the read-only and the edit profile at their own
prompt-relative position instead of restating that block. Adapter tests should
pin exact Spec, Execute, and Review argv; prove read-only controls for
Spec/Review; and install a fixture entry point from an isolated path. A plugin that declares structured usage must record only numeric aggregate
token fields before `machinist report` includes them.

## Compatibility checks

Harness CLIs evolve independently. Before unattended use after an upgrade:

```sh
machinist doctor
```

The compatibility rows execute `--help` against the exact configured Spec,
Execute, and Review argv without starting a Harness Task. If an argument changes, update
the adapter, its exact argv test, this matrix, and the changelog together.

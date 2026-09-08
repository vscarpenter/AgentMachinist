# Harness support matrix

AgentMachinist includes four built-in adapters and discovers installed v1
plugins. The guided local workflow requires executable adapters for all three
Phases; the first-run selector accepts a full-pipeline adapter. Phase profiles
may subsequently select different supported adapters. “Spec and Review control”
describes adapter arguments; the controller also checks repository custody and
rejects changes from these read-only Phases.

This matrix covers AgentMachinist 0.15.0, including the local workflow, GitLab
publication, and the optional local readiness new in 0.15.0. The CI column
below describes the existing GitHub Actions Spec
workflow, not GitLab CI.

| Config value | Executable | Spec and Review control | Implementation control | Managed Spec CI secret |
| --- | --- | --- | --- | --- |
| `claude-code` | `claude` | Plan permission mode, Read/Grep/Glob tools, no session persistence | Edit mode with gate commands allowlisted and no session persistence; prompt plus Git postconditions | `ANTHROPIC_API_KEY` |
| `codex` | `codex` | Read-only sandbox and ephemeral session | Workspace-write sandbox, approval prompts disabled, ephemeral session; prompt plus Git postconditions | `OPENAI_API_KEY` |
| `pi` | `pi` | Read/grep/find/ls allowlist; extensions, skills, prompt templates, and sessions disabled | Normal print-mode tools with session persistence disabled; prompt plus Git postconditions | `GEMINI_API_KEY` |
| `opencode` | `opencode` | Pure plan agent; treated as advisory | Normal run agent; prompt plus Git postconditions | `ANTHROPIC_API_KEY` by default |

Review is a separate durable Task Run even when it inherits the same adapter.
It receives the approved Spec, diff, verification Evidence, and Task context,
and runs under the adapter's read-only Review argv. Its version-1 findings are
advisory. Local Review is mandatory: invalid output, mutation, or a changed
candidate prevents integration/publication eligibility. In the GitHub issue
pipeline, `review.enabled` controls Review; when enabled, failure leaves the
PR draft. A changed successful Execute SHA permits another Review, while a
completed Review for the same candidate is not repeated.

## Verification feedback loop

When gates are configured and `verification.harness_may_run_gates` is true
(the default), the implementation prompt lists each gate command and asks the
harness to run required gates and iterate until they pass before finishing.
`codex`, `pi`, and `opencode` execute modes already permit command execution,
so only the prompt changes for them. `claude-code`'s headless edit mode
denies commands, so the adapter additionally allowlists the configured gate
commands and variants with additional arguments
(`--allowedTools "Bash(<command>)" "Bash(<command>:*)"`). The controller's own
gate run afterwards stays authoritative.

## Authentication

Runs use the Harness's existing provider authentication. Local setup checks
installed executables and Phase support; it does not run the provider's login
probe or validate model access. Use the checks below before your first Task.
Plain `doctor` checks the installed version, parses configured Spec and Execute
invocations, and checks Review when `review.enabled: true` for the GitHub
workflow. **New in 0.15.0:** optional `doctor --local` uses the
same probes with the local settings that `start` resolves, including mandatory
Review. These diagnostics use the adapter's read-only authentication probe when one is available;
plugins without a probe require manual verification. A successful probe
confirms configured credentials, not subscription quotas or access to every
possible model.

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
its value. A third-party adapter without `ci_spec` metadata cannot run managed
GitHub Spec CI; workflow projection fails with a recovery choice instead of
guessing. It can still support foreground local Tasks or the local GitHub
watcher if its declared Phases are sufficient. GitLab issue import/publication
uses `glab` independently and does not install a hosted Spec workflow.

## Credential environment

Harness subprocesses retain provider variables such as `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY` through an explicit allowlist. Other provider or plugin keys
are not automatically preserved: common secret-name suffixes and cloud
credential variables are filtered. AgentMachinist also removes common forge
tokens, Git askpass, and SSH-agent variables and disables terminal credential
prompting. This is credential reduction, not credential isolation; see the
[trust model](trust-model.md).

## Model and additional arguments

`harness.model` passes one model selection to the adapter. `harness.extra_args`
is an advanced option applied to Spec, Execute, and Review. For the four built-in
adapters, AgentMachinist rejects reserved sandbox, permission, model, session,
and tool flags, including duplicate forms that could override its controls.
Third-party adapters do not inherit that reserved-argument map and must validate
their own controls. Other additional arguments are appended to the adapter
command and may change behavior as harness CLIs
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
token fields before `machinist report` includes them. That aggregate report
currently reads legacy issue-run history; use the printed local Task report
and `machinist status T1 --json` for the foreground workflow.

## Compatibility checks

Harness CLIs evolve independently. Before unattended GitHub watcher use after
an upgrade:

```sh
machinist doctor
```

The compatibility rows execute `--help` against the configured Spec and Execute
argv, plus Review when enabled, without starting a Harness Task. If an argument
changes, update the adapter, its exact argv test, this matrix, and the changelog together.
Plain `doctor` reads root `machinist.yaml` and checks GitHub setup. The
0.15.0 `machinist doctor --local` option reads saved local settings or previews
first-start discovery without saving it. It runs no model, forge, release-update probe, or Verification
Gate by default. `--run-gates` explicitly runs project commands in the
controller checkout, where they may write or download; passing does not prove
the isolated Workshop baseline. See [local readiness](local-workflow.md#optional-local-readiness).

For foreground Tasks, inspect the saved configuration via
`machinist config show --path .machinist/runs/local/config.yaml`, check the
selected Harness's authentication, and use `machinist rehearse --harness` only
when you intend to invoke its configured profiles in a disposable repository.
Plain `machinist rehearse` exercises the production local controller with a
fake Harness and no model cost.

Local orchestration does not select a local model. A configured adapter may use
a cloud provider. An offline setup needs a provider/adapter combination that
supports local inference, downloaded models and dependencies, and a separately
verified run with network access denied.

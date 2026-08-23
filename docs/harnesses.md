# Harness support matrix

AgentMachinist supports four local adapters. “Spec control” describes the
adapter arguments; all adapters also face the controller's dirty-tree check.

| Config value | Executable | Spec control | Implementation control | CI template |
| --- | --- | --- | --- | --- |
| `claude-code` | `claude` | Plan permission mode, Read/Grep/Glob tools, no session persistence | Edit mode with gate commands allowlisted; prompt plus Git postconditions | Supported |
| `codex` | `codex` | Read-only sandbox and ephemeral session | Full-auto workspace edits; prompt plus Git postconditions | Local only |
| `pi` | `pi` | Read/grep/find/ls allowlist; extensions, skills, prompt templates, and sessions disabled | Normal print-mode tools; prompt plus Git postconditions | Local only |
| `opencode` | `opencode` | Pure plan agent; treated as advisory | Normal run agent; prompt plus Git postconditions | Local only |

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

Local runs use the harness's existing provider authentication. AgentMachinist
does not validate subscription state or model access; `doctor` verifies only
that the executable is discoverable. Run the harness interactively once if an
otherwise healthy setup exits with an auth error.

Current authentication entry points are:

| Harness | Check | Sign in or configure |
| --- | --- | --- |
| Claude Code | `claude auth status` | `claude auth login` |
| Codex | `codex login status` | `codex login` |
| OpenCode | `opencode auth list` | `opencode auth login` |
| Pi | `pi auth check --model <model>` | Configure credentials for the selected provider or model, then rerun the check. |

These CLIs evolve independently. Confirm the command with the installed
harness's `--help` output when upgrading.

The GitHub Actions spec template installs Claude Code and requires
`ANTHROPIC_API_KEY`. Selecting another local harness does not rewrite that
provider-specific installation step.

## Credential environment

Harness subprocesses retain provider variables such as `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`. AgentMachinist removes its common GitHub token, Git askpass,
and SSH-agent variables and disables terminal credential prompting. This is
credential reduction, not credential isolation; see the
[trust model](trust-model.md).

## Model and additional arguments

`harness.model` passes one model selection to the adapter. `harness.extra_args`
is an advanced option applied to both Spec and Execute. AgentMachinist rejects
adapter-owned sandbox, permission, model, session, and tool flags, including
duplicate forms that could override its controls. Other additional arguments
are appended to the adapter command and may change behavior as harness CLIs
evolve, so keep `extra_args` empty unless you have reviewed the final command
and updated your threat assessment.

## Compatibility checks

Harness CLIs evolve independently. Before unattended use after an upgrade:

```sh
claude --help
opencode run --help
pi --help
codex exec --help
uv run pytest tests/test_harness.py
```

If an argument changes, update the adapter, its exact argv test, this matrix,
and the changelog in the same change.

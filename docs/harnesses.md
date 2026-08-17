# Harness support matrix

AgentMachinist supports four local adapters. “Spec control” describes the
adapter arguments; all adapters also face the controller's dirty-tree check.

| Config value | Executable | Spec control | Implementation control | CI template |
| --- | --- | --- | --- | --- |
| `claude-code` | `claude` | Plan permission mode, Read/Grep/Glob tools, no session persistence | Edit mode; prompt plus Git postconditions | Supported |
| `codex` | `codex` | Read-only sandbox and ephemeral session | Full-auto workspace edits; prompt plus Git postconditions | Local only |
| `pi` | `pi` | Read/grep/find/ls allowlist; extensions, skills, prompt templates, and sessions disabled | Normal print-mode tools; prompt plus Git postconditions | Local only |
| `opencode` | `opencode` | Pure plan agent; treated as advisory | Normal run agent; prompt plus Git postconditions | Local only |

## Authentication

Local runs use the harness's existing provider authentication. AgentMachinist
does not validate subscription state or model access; `doctor` verifies only
that the executable is discoverable. Run the harness interactively once if an
otherwise healthy setup exits with an auth error.

The GitHub Actions spec template installs Claude Code and requires
`ANTHROPIC_API_KEY`. Selecting another local harness does not rewrite that
provider-specific installation step.

## Credential environment

Harness subprocesses retain provider variables such as `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`. AgentMachinist removes its common GitHub token, Git askpass,
and SSH-agent variables and disables terminal credential prompting. This is
credential reduction, not credential isolation; see the
[trust model](trust-model.md).

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

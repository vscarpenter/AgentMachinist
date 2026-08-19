# Trust model

AgentMachinist is designed for repositories and harness installations you
already trust. It improves custody and failure visibility; it is not an OS
sandbox, container boundary, malware scanner, or policy engine.

## Trusted inputs and principals

- The repository's default branch, config, prompts, hooks, and test command.
- The installed harness executable and its provider/plugin ecosystem.
- Repository owners, members, and collaborators who can approve.
- The local user account that launches AgentMachinist.

Issue bodies and PR branches are untrusted task input. `pull_request_target`
approval automation never checks out or executes PR-head code.

## Enforced controls

- Exact SHA-bound approval plus configured label.
- Exact `/machinist-execute <full-spec-commit-sha>` command and trusted author
  association; label approvals bind the SHA from the authorization event.
- Codex read-only sandbox, Pi read-tool allowlist, and Claude plan/read-tool
  arguments during spec generation.
- Rejection of any dirty repository after spec generation.
- Post-implementation checks for harness-created commits, changed remote branch
  heads, and edits under `.machinist/`.
- Push lease against the approved head SHA.
- Test gate before push when `tests.command` is configured.
- Atomic local Task Run records and explicit retry.

“Enforced” here means AgentMachinist or the selected CLI checks it. It does not
mean a hostile process with the same OS identity cannot work around it.

## Advisory or detective controls

- The implementation prompt says not to run Git or edit `.machinist/`.
- OpenCode plan-agent write behavior is treated as advisory.
- Git postconditions detect ordinary violations after the harness exits; they
  cannot undo an external side effect.
- Removing `GH_TOKEN`, `GITHUB_TOKEN`, askpass variables, and the SSH agent from
  the harness environment reduces ambient controller authority. Provider keys
  remain available. Other credentials—keychain helpers, SSH keys on disk,
  cloud credentials, or tokens loaded by plugins—may still be reachable.

Therefore, documentation must not claim that a harness “has no Git access.”

## Test command

The test command is repository-controlled shell text and runs as the local
user. A null command skips the gate. A passing command proves only what that
suite covers; it is not runtime, deployment, or security proof.

## Recommended deployment boundary

For higher-risk repositories, run AgentMachinist in a dedicated OS account or
ephemeral VM/container with scoped GitHub credentials, no unrelated cloud
credentials, and network controls appropriate to the harness provider. Keep
merge protection and required CI reviews on the repository.

## Residual risks

- A compromised harness can read files available to the launching user.
- A repository test or hook can execute arbitrary code.
- Cross-host duplicate execution is not prevented by the local claim.
- A harness-side remote effect can be detected without being reversible.
- Model output can be wrong while tests pass.

The final control is still human review plus repository branch protection.

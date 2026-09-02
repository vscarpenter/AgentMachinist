# Trust model

AgentMachinist is designed for repositories and harness installations you
already trust. It improves custody and failure visibility; it is not an OS
sandbox, container boundary, malware scanner, or policy engine.

## Trusted inputs and principals

- The repository's default branch, config, prompts, hooks, and test command.
- The installed harness executable and its provider/plugin ecosystem.
- Repository actors with write or admin access who can approve.
- The local user account that launches AgentMachinist.

Issue bodies and PR branches are untrusted task input. `pull_request_target`
approval automation never checks out or executes PR-head code.

## Enforced controls

- Exact SHA-bound approval plus configured label.
- Exact `/machinist-execute <full-spec-commit-sha>` command and trusted author
  association; label approvals bind the SHA from the authorization event.
- Actor authorization on both approval paths. Both comment and label approval
  paths independently require write or admin access. The comment path also
  requires OWNER, MEMBER, or COLLABORATOR association. GitHub association and
  label permissions can be weaker than push authority, so neither is
  sufficient by itself. The check fails closed: an unreadable permission mints
  no approval evidence. The approver's login is recorded on the approval
  comment.
- Repository custody binds GitHub operations to the controller origin's host,
  owner, and repository, then checks the expected same-repository PR number,
  base, head, state, and draft status before a Phase changes it.
- Codex read-only sandbox, Pi read-tool allowlist, and Claude plan/read-tool
  arguments during spec generation.
- Rejection of any dirty repository after spec generation.
- Post-implementation checks for harness-created commits, changed remote branch
  heads, and edits under `.machinist/`.
- Rejection of deleted test files (heuristic path patterns; renames count as a
  deletion) unless `limits.allow_test_deletions` is set. Modifying a test is
  not detectable this way — weakened tests still need human review.
- Git metadata custody: the Workshop's `.git` pointer, config, hooks, and
  alternates are fingerprinted before the harness runs and re-checked before
  every later Git call. See Git metadata custody below.
- Push lease against the approved head SHA.
- Required verification gates before push when `tests.command` or named
  `verification.gates` are configured.
- Atomic local Task Run records and explicit retry.
- A separate read-only Review Task Run must validate and comment on the exact
  delivered implementation head before AgentMachinist marks it ready.

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

Independent Review reduces producer self-evaluation risk, but it is not a
security scanner or merge authorization. Findings are advisory, the reviewer
is still local software running as the same OS user, and a configured Review
profile may use the same provider as Execute. Human code review remains the
final gate.

Harness plugins are trusted installed Python code. Entry-point validation
prevents name collisions and isolates broken imports; it cannot constrain what
a successfully imported plugin does.

## Git metadata custody

Before an untrusted phase starts, the controller fingerprints the Workshop's
Git metadata: the `.git` pointer, `commondir`, config files, `info/`,
`objects/info/alternates`, `shallow`, `refs/replace`, and every hook. It
re-checks that fingerprint before each later Git call, and the check runs
before the first Git subprocess so a planted `core.fsmonitor`, clean filter,
or hook never gets a process to execute in.

Config files shared with the controller repository under
`workspace.strategy: worktree` are compared by security-sensitive key. A
change trips the guard when it touches a key that can execute a program, name a
path Git will trust, or redirect the network: `core.fsmonitor`,
`core.hooksPath`, `core.pager`, `core.sshCommand`, `core.worktree`,
`filter.*.clean`, `filter.*.smudge`, `diff.*.command`, `alias.*`,
`credential.*`, `url.*`, `include.path`, `remote.origin.url`, and the rest of
the same family. Editing `diff.tool`, `user.name`, or adding a second remote
does not trip a shared-config comparison. A clone Workshop owns its config, so
that file is compared byte for byte. An unreadable config fails custody rather
than being assumed safe.

Hooks, `info/`, and `objects/` stay compared byte for byte. A hook body is
code, so there is no benign subset to carve out.

**`workspace.strategy: worktree` shares this metadata with your own
repository.** A Git worktree gets its own `HEAD`, index, and refs, and shares
`config`, `hooks/`, `info/`, and `objects/` with the parent. So the watched
config is your main repository's, and installing a hook or a `core.pager` in
your own checkout while a Task runs will stop that Task. Use
`workspace.strategy: clone` when you want each Workshop to own its Git
metadata outright.

Task Run records store hashes of sensitive config values rather than the
values, so a credentialed origin or an `http.*.extraheader` never lands in a
run record or an error message.

## Verification commands

The legacy test command and every named verification gate are
repository-controlled shell text and run as the local user. A null
`tests.command` skips only the legacy command; verification is skipped when no
named gates are configured either. A passing command proves only what that
suite covers; it is not runtime, deployment, or security proof.

By default the implementation harness is told the gate commands and may run
exactly those commands itself to iterate before it finishes
(`verification.harness_may_run_gates`). This grants no execution capability
the pipeline does not already exercise: the controller runs the same
repository-controlled commands on harness-authored code immediately
afterwards, and that controller run remains the authoritative gate. Set
`verification.harness_may_run_gates: false` to withhold both the commands and
(for `claude-code`) the corresponding `--allowedTools` grants.

## Telemetry

Local reporting reads Task Run history but emits aggregates rather than raw
Evidence. OTLP export is disabled by default and constructs its payload from an
allowlist: repository identity, phase, status, Harness, model, counts, rates,
and duration statistics. Issue bodies, prompts, source/diffs, commands, error
messages, arbitrary Evidence, environment values, and credential values are
not export inputs. Authorization is read only from
`MACHINIST_OTLP_AUTHORIZATION` and is rejected on malformed or credentialed
endpoint URLs.

An operator who configures an endpoint is trusting that collector with the
allowlisted repository identity and usage aggregates. Use HTTPS and the same
network isolation expected for other observability traffic.

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
- Independent Review can miss a defect or produce a false-positive advisory.
- An explicitly configured telemetry collector learns repository identity and
  aggregate operational behavior.
- Under `workspace.strategy: worktree`, Git metadata custody covers a
  directory you also edit, so the guard reports your own changes as well as
  a harness's.

The final control is still human review plus repository branch protection.

# Trust model

AgentMachinist is designed for repositories and harness installations you
already trust. It improves custody and failure visibility; it is not an OS
sandbox, container boundary, malware scanner, or policy engine.

## Trusted inputs and principals

- The repository's default branch, config, prompts, hooks, and test command.
- The installed harness executable and its provider/plugin ecosystem.
- The local operator who approves a local Spec and explicitly integrates it.
- Repository actors with write or admin access who can authorize legacy GitHub
  execution or review/merge published changes.
- The local user account that launches AgentMachinist.

Task bodies, imported issues, and PR/MR branches are untrusted input. `pull_request_target`
approval automation never checks out or executes PR-head code.

## Local Approval, integration, and publication

The foreground local workflow and GitLab intake/publication are available in
AgentMachinist 0.14.0 alongside the existing GitHub issue workflow.

The local journey records an explicit human Approval tied to repository, Task,
exact Spec SHA, actor, and time. Copying a Task record to another repository or
changing its Spec does not carry valid Approval. Local records remain editable
by the same OS account; they are workflow Evidence, not an authentication
boundary against hostile local processes. Forge review buttons and GitLab
comments do not mint local Approval.

Every local candidate requires at least one configured required Verification
Gate and completed independent Review
of its exact SHA. Findings are advisory and the human must inspect the change.
`machinist integrate T1` is explicit, requires the clean expected base and
candidate on the originally selected named base branch, and permits only
fast-forward integration. It records intent and
observed completion for recovery; it does not authorize automatic or remote merge.

Optional publication binds the authenticated forge client to the Git origin's
host and repository, with exactly one origin URL and no separate push URL or
Git URL rewrites. It refuses unowned existing branches, persists its push
intent and expected remote SHA, leases the push, and verifies exact open PR/MR
identity afterward. Publication retries use the saved candidate and do not
repeat model work or successful verification. Local Tasks need no forge, but
the Harness can still contact a cloud provider; no offline-model guarantee is
implied. GitLab issue import and publication use `glab` bound to the selected
host, including nested project paths and self-managed instances. GitLab CI and
remote Approval are not part of this contract. An imported issue's source is
provenance; it cannot redirect the later publication target.

Local Task records are tied to the canonical controller repository and live in
`.machinist/runs/local/`, separate from legacy issue runs. Local setup writes
Git's local runtime exclusion and a saved configuration; it does not grant the
Harness authority over these files. Backups of this runtime state contain
Task bodies, Approval, and detailed Evidence and deserve the same treatment as
the repository's source.

## Enforced controls

The following GitHub label/comment controls apply to the legacy issue pipeline;
local Approval uses the repository/Task/Spec record described above. Git custody,
verification, Task Run persistence, and read-only Review apply to both paths.

- Exact SHA-bound GitHub approval plus configured label.
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
- Post-implementation checks for Harness-created commits and edits under
  `.machinist/`. Local Phases also compare controller/Workshop HEAD, branch,
  local refs, and custody. Live remote-head checks belong to legacy GitHub
  Phases and optional publication; foreground local Phases do not query a forge.
- Rejection of deleted test files (heuristic path patterns; renames count as a
  deletion) unless `limits.allow_test_deletions` is set. Modifying a test is
  not detectable this way — weakened tests still need human review.
- Git metadata custody: the Workshop's `.git` pointer, config, hooks, and
  alternates are fingerprinted before the harness runs and re-checked before
  every later Git call. See Git metadata custody below.
- Legacy push lease against the approved head SHA; optional local publication
  lease against the Task's persisted remote expectation.
- Required verification before local candidate delivery. The legacy GitHub
  workflow enforces configured `tests.command` or named `verification.gates`
  before push, but permits an explicitly ungated configuration.
- Atomic local Task Run records and explicit retry.
- In the GitHub issue pipeline with `review.enabled: true`, a separate
  read-only Review Task Run must validate and comment on the exact delivered
  implementation head before AgentMachinist marks it ready. Local Review produces its report before
  explicit integration or optional publication becomes eligible.

“Enforced” here means AgentMachinist or the selected CLI checks it. It does not
mean a hostile process with the same OS identity cannot work around it.

## Advisory or detective controls

- The implementation prompt says not to run Git or edit `.machinist/`.
- OpenCode plan-agent write behavior is treated as advisory.
- Git postconditions detect ordinary violations after the harness exits; they
  cannot undo an external side effect.
- Removing common forge tokens, including `GH_TOKEN`, `GITHUB_TOKEN`, and
  `GITLAB_TOKEN`, askpass variables, and the SSH agent from
  the harness environment reduces ambient controller authority. Explicitly
  allowlisted provider keys remain available; other common secret names and
  cloud credential variables are filtered. Other credentials—keychain helpers,
  SSH keys on disk, cloud credentials, or tokens loaded by plugins—may still
  be reachable.

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

Fingerprinted hooks, `info/` metadata, and object alternates stay compared byte
for byte. A hook body is
code, so there is no benign subset to carve out.

**`workspace.strategy: worktree` shares this metadata with your own
repository.** A Git worktree gets its own `HEAD`, index, and per-worktree refs,
and shares branch refs, `config`, `hooks/`, `info/`, and `objects/` with the parent. So the watched
config is your main repository's, and installing a hook or a `core.pager` in
your own checkout while a Task runs will stop that Task. Use
`workspace.strategy: clone` when you want each Workshop to own its Git
metadata outright.

Task Run records store hashes of sensitive config values rather than the
values, so a credentialed origin or an `http.*.extraheader` never lands in a
run record or an error message.

## Verification commands

**Unreleased / source checkout:** optional `machinist doctor --local` resolves
the settings `start` would use and checks local Git readiness, Harness
availability and supported version/help/authentication probes, and required
Gate command entry points. By default the controller writes no Task, Workshop,
runtime/config file, exclusion, or ref and invokes no model, forge, update
probe, or Gate. Installed adapters and their probe commands remain trusted
local code; a successful authentication check does not establish model access
or quota. This option is not included in published 0.14.0.

`machinist doctor --local --run-gates` explicitly executes configured project
commands through the shared Verification engine in the controller checkout.
Commands may write files or download dependencies. Passing there is not proof
of the isolated Workshop baseline; `start` still checks that baseline before
Spec generation. Plain `doctor` retains its GitHub setup scope.

The legacy test command and every named verification gate are
repository-controlled shell text and run as the local user. A null
`tests.command` skips only the single legacy command. In the GitHub issue
workflow, verification is skipped when no named Gates are configured either.
Foreground local setup instead requires at least one required Gate and runs
baseline verification before Spec generation; disabling local Review or required
verification is rejected on configuration load. A passing command proves only
what that suite covers; it is not runtime, deployment, or security proof.

By default the implementation Harness is told the Gate commands and asked to
run them itself to iterate before it finishes
(`verification.harness_may_run_gates`). Claude Code receives explicit allow
rules for those commands and variants with additional arguments; the other
built-in Execute adapters already permit command execution. The setting does
not impose a command allowlist on those adapters. This grants no execution
capability the pipeline does not already exercise: the controller runs the same
repository-controlled commands on harness-authored code immediately
afterwards, and that controller run remains the authoritative gate. Set
`verification.harness_may_run_gates: false` to withhold both the commands and
(for `claude-code`) the corresponding `--allowedTools` grants.

## Diagnostic output

**Unreleased / source checkout:** a shared renderer bounds Git, `gh`, `glab`,
and doctor diagnostics, strips unsafe terminal controls, and redacts recognized
URL credentials, authorization values, and secret assignments. It preserves
useful multiline context and marks truncation. Unrecognized secret forms can
still appear; this is not a guarantee that arbitrary output is secret-free.
Stored raw logs and general command output are not sanitized by this contract.
Inspect logs and Evidence before sharing them.

## Telemetry

`machinist report` reads legacy issue Task Run history but emits aggregates
rather than raw Evidence. OTLP export is disabled by default and constructs its payload from an
allowlist: repository identity, phase, status, Harness, model, counts, rates,
and duration statistics. Issue bodies, prompts, source/diffs, commands, error
messages, arbitrary Evidence, environment values, and credential values are
not export inputs. Authorization is read only from
`MACHINIST_OTLP_AUTHORIZATION` and is rejected on malformed or credentialed
endpoint URLs.

Foreground local configuration requires telemetry export disabled. Its status
and Markdown report remain on disk; the aggregate report command does not
include that local Task namespace.

An operator who configures an endpoint is trusting that collector with the
allowlisted repository identity and usage aggregates. Use HTTPS and the same
network isolation expected for other observability traffic.

## Recommended deployment boundary

For higher-risk repositories, run AgentMachinist in a dedicated OS account or
ephemeral VM/container with scoped forge credentials, no unrelated cloud
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

Human review remains necessary for local integration. Published changes also
need the forge's branch protection and remote review/merge policies.

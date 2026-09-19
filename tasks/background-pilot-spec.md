# Background Task pilot

Status: implemented and locally verified on `codex/background-pilot`.
The live trial still requires a selected repository and working Docker runtime.

## Outcome

A trusted user queues a GitHub issue and receives one implementation PR without
an intermediate Spec-SHA Approval or publication prompt. Human review and merge
remain the delivery boundary. This is an opt-in pilot in the existing controller.

## Scope and contracts

- Build on local Tasks and TaskDispatcher. Keep legacy and manual workflows
  compatible. Never represent bot delegation as human Approval.
- A separate `delegation` Task record binds repository, Task, issue/event identity,
  request snapshot, actor, base commit, effective configuration digest, and the
  internal Spec once generated. A valid delegation authorizes Execute, Review,
  and PR publication under its saved policy. Manual integration remains explicit.
- Queue intake uses the configured label and verifies the labeling actor has
  write/admin permission. Freeze issue content at admission. Repeated events and
  restarts find the same Task; changed issue content does not alter accepted work.
- One persistent worker per repository, with an exclusive worker claim. Durable
  local state controls admission, deadlines, outcomes, publication recovery, and
  notification deduplication. A stopped or cancelled task needs explicit retry.
- Background Harness and Verification processes run in disposable Docker
  containers mounting only the individual clone Workshop. No controller state,
  host home, Docker socket, or publication credentials enter those containers.
  Provider credentials are passed only to Harness processes. Network access is
  explicit; containers do not imply an egress firewall. Require a working runtime
  and configured image before accepting paid work; never fall back to host execution.
- Use existing Spec, Execute, required gates, one eligible configured local repair,
  and separate exact-candidate Review. All active work shares a persisted deadline.
- Protect worker/verification configuration and managed workflow paths using
  controller-enforced limits. Scope restrictions are output checks, not a claim
  that prompts constrain arbitrary code execution.
- Publish at most one PR per task. Start as draft while CI is pending or there are
  high-severity Review findings. Observe required checks on the exact candidate;
  missing, failing, or unknown checks cannot become success. Remote CI failure
  produces a needs-attention result; no remote CI or review-driven repair in v1.
- Cap outstanding bot PRs (default two), run concurrency one, and default task
  runtime to 30 minutes. Completed, failed, or needs-attention notifications are
  deduplicated; ordinary progress remains in logs/status. Cancellation is checked
  before publication and ready transitions.
- Provide `machinist background` commands for a one-shot or persistent worker,
  readiness, status, cancellation and explicit retry. Commands are opt-in and do
  not install/start a service automatically.
- Existing user docs, trust guidance, schema, changelog, and CLI help must explain
  manual versus delegated Tasks and distinguish local verification, remote CI,
  automated Review completion, and human acceptance.

## Validation and rollout

Start with failing behavior contracts. Exercise real Git with injected Harness
and GitHub transports, duplicate intake, interruptions, consumed budgets,
cancel-before-publish, stale configuration/Spec, draft findings, and CI identity.
Run a controlled Docker smoke where available and the full canonical gates.
Obtain an independent review of the final diff and fix valid findings.

The first real-project target is pending user selection. Never claim the pilot
saved time or a live provider/remote run succeeded based on fake transports.
Prepare a simple trial record for 10–15 real tasks comparing active preparation,
review, intervention, maintenance, and rework against direct-agent use.

## Deferred

Automatic merge/deployment, automatic task discovery, distributed workers,
ephemeral CI worker fleets, dashboards, multi-forge background support, additional
repair loops, and changing existing installations to unattended operation.

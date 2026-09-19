# Background Task pilot

**Unreleased, source checkout only.** The published AgentMachinist 0.17.1
package does not include these commands. The existing
[manual workflow](local-workflow.md) remains available.

Queue a bounded GitHub issue and receive one implementation PR without an
intermediate Spec-SHA Approval or publication prompt:

```text
Trusted queue action → internal Spec → Execute → local Verification
                          → independent Review → draft PR → required CI
                                                          → human review and merge
```

The controller owns commits, publication, and PR transitions. It retains the
internal Spec and exact code identities as Evidence. Automated Review is a
separate machine pass; you still review the finished change and decide whether
to merge it. Nothing merges or deploys automatically.

This pilot tests whether unattended delivery saves your attention. Start with
one smaller repository, Codex, and tasks with observable acceptance tests.
Use the [trial worksheet](background-trial.md) to compare 10–15 real tasks with
your usual agent workflow. Passing repository tests is not evidence of measured
time savings or a successful live provider run.

## Prepare one worker

Use one persistent controller checkout per repository. Its Task records, worker
claim, intake history, and publication recovery must survive process restarts.
Separate laptops or controller checkouts are not coordinated workers. The pilot
does not provision an ephemeral GitHub Actions fleet or install a service.

The controller host needs:

- Python 3.12 or later, Git, and the current AgentMachinist source installation.
- Docker with a running engine and the configured worker image already built.
- Authenticated `gh` with access to issues, collaborator permissions, checks,
  and PR publication on the selected repository.
- A configured Git author, a clean named base branch, and an `origin` matching
  that repository.
- `OPENAI_API_KEY` for Codex. The pilot supports Codex for all three Phases;
  host desktop login state is not mounted into the container.

From the AgentMachinist source checkout, install the controller and build the
image:

```sh
uv tool install --force --editable .
docker build -t machinist-worker:pilot scripts/background-worker
```

This replaces any installed AgentMachinist tool with the editable source checkout.
For this source trial, keep using that source installation. A normal upgrade to
published 0.17.1 does not supply background operation. Build changes to the image
before restarting the worker. Provision project-specific compilers and tools in
your own image if the supplied image does not contain them.
Custom images can add build tools; they do not enable other Harness adapters.

Provide `OPENAI_API_KEY` through the controller process's environment using your
existing secret-management method. Keep it out of tracked files. The runtime
maps it to `CODEX_API_KEY` for Codex execution and passes it only to Harness containers. Controller
GitHub credentials do not enter those containers; separately supervised
Verification containers receive no provider key.

Background Codex relies on Docker for execution isolation. Its container-only
profile uses `--sandbox danger-full-access` and `approval_policy="never"` inside
that boundary, avoiding nested Codex/Bubblewrap namespace requirements. Docker's
security restrictions remain enabled. Spec and Review mount the Workshop
read-only; Execute mounts it writable. Gates also use a writable mount, with
the configured mutation policy enforced by the controller. The ordinary host
Codex adapter retains its read-only/workspace-write sandbox profiles.

The repository's GitHub authentication must allow ordinary PR checks to run on
the published commit. If you move the controller into Actions later, account for
the event restrictions of its default `GITHUB_TOKEN`; this pilot does not install
or configure a GitHub App for you.

## Configure the target repository

Background commands read root `machinist.yaml` by default. They do **not** read
the foreground copy at `.machinist/runs/local/config.yaml`. To choose another
file, put the option on the group, as in
`machinist background --config path/to/config.yaml doctor`.

Use an existing compatible configuration or create one. This Python example
requires a committed lockfile and a CI check named `tests`; replace the gate and
check names with the commands and exact GitHub check names for your project:

```yaml
version: 1
harness:
  name: codex
github:
  manage_workflows: false
workspace:
  strategy: clone
tests:
  command: uv run pytest
verification:
  repair:
    max_attempts: 1
review:
  enabled: true
background:
  enabled: true
  queue_label: machinist:queue
  image: machinist-worker:pilot
  network: bridge
  required_checks: [tests]
  timeout_minutes: 30
  max_open_prs: 2
  poll_interval_seconds: 60
```

Keep `harness.name: codex`; any Spec, Execute, or Review profile override must
also select Codex. Readiness rejects other adapters for this pilot.

If your configuration already uses named `verification.gates`, keep those
instead of adding `tests.command`; the two forms cannot be combined. At least
one required local Gate is necessary. Baseline Verification runs before the
Spec Harness, and authoritative Gates run after implementation. Ignored host
dependencies are not copied into the Workshop. Commands must prepare their own
dependencies or use tools installed in the image.

`background.enabled` defaults to `false`. Enabling it requires an image and a
nonempty list of required remote checks. The runtime always uses clone Workshops
for background Tasks and adds protected paths to configured change limits:
`.machinist/`, `.github/workflows/`, `machinist.yaml`, `AGENTS.md`, and `CLAUDE.md`.
Task output cannot change these files. Apply worker configuration or workflow
changes through your ordinary reviewed development process.

The default task limit is 30 minutes; configuration accepts 1–120. The PR cap
defaults to two and accepts 1–10. The polling interval defaults to 60 seconds and
accepts 5–3600. Execution concurrency is always one. The deadline covers active
Task work, including eligible repair. Repair remains optional and permits at
most one extra invocation inside Execute; it does not repair remote CI or Review
findings.

Docker `bridge` networking allows provider and dependency access. It is not an
egress allowlist. `none` disables container networking and is unsuitable for a
cloud Codex invocation. Choose any additional network restrictions outside
AgentMachinist. See the [trust model](trust-model.md#background-execution-boundary).

Commit the setup and start from a clean named controller branch before queuing
work. Background Tasks fetch the GitHub repository's default branch as their
base; they do not use an arbitrary controller feature branch as task input.
The controller does not generate or require the legacy Spec/Approval workflows
for this path. Existing managed workflows retain their separate behavior.

## Queue and run one Task

Create the queue label once in the target repository, then check worker
readiness:

```sh
gh label create machinist:queue --description "Delegate a bounded Task to AgentMachinist"
machinist background doctor
```

Doctor checks readiness; it does not demonstrate that a provider will solve a
task or that the project's Gates pass in the image. Runtime probes inspect the
daemon and prebuilt image; they do not pull an image, authenticate a provider,
or execute a test container. Resolve failures before
accepting paid work. There is no fallback to running task code on the host when
Docker is unavailable.

Write a small GitHub issue containing the problem, acceptance criteria, and any
important scope limit. A user with write or admin access applies the queue label:

```sh
gh issue edit 12 --add-label machinist:queue
machinist background run --once
```

The controller verifies the actual labeling actor's permission, not the issue
author or the mere presence of a label. Accepted work receives a local ID such
as `T1`. Delegation freezes the issue content, source event, actor, repository,
base commit, and policy identity. Later issue edits do not redirect that Task.
Duplicate events, polling, and re-labeling an accepted issue reuse the recorded
intake rather than repeat paid work. To change the request after admission,
cancel it and delegate a new issue.

The one-shot command performs a worker pass and exits unsuccessfully if its
results include failed, cancelled, or needs-attention work. To keep polling for queued Tasks
and PR check results, run:

```sh
machinist background run
```

Keep that process and its persistent checkout available. Use your own service
manager if needed; the existing `machinist service` commands schedule the legacy
watcher and do not install this background worker.

## Inspect, cancel, and recover

```sh
machinist background status
machinist background status T1
machinist background status --json
machinist background cancel T1
```

Cancellation cooperatively stops supervised work and is checked before
publication and ready transitions. It does not undo a push or delete an existing
PR. Removing a queue label or editing the issue is not a replacement for the
explicit cancellation command after admission.

Failed, cancelled, or interrupted work stays stopped until explicit retry:

```sh
machinist background retry T1
machinist background run --once
```

Inspect the failure and retained Evidence first. Retry schedules the failed,
cancelled, or needs-attention Task for the next worker pass with a new configured
deadline. It requires the saved policy to remain unchanged and operates on the
saved request; it does not silently adopt edited issue text. Ordinary foreground
continuation and retry refuse delegated Tasks. Publication
recovery reconciles the existing candidate, branch, and PR rather than rerunning
successful machine Phases. Preserve the controller's runtime records: deleting
them removes the evidence needed for duplicate prevention and recovery.

Status also shows the source issue number. If intake failed before a local Task
ID could be allocated, select that saved issue explicitly:

```sh
machinist background retry --issue 12
```

This also schedules work for a subsequent pass; reapplying the queue label does
not reset a stopped attempt.

## Read the result

A completed local candidate is first published as a draft. The PR summarizes the
change and carries local Verification and independent Review Evidence. The
worker observes required GitHub checks for the **exact published candidate**.
It does not substitute a successful result from another commit, treat a missing
check as success, or infer correctness from Review completion.

- Successful required checks and no high-severity Review findings allow the
  controller to mark the PR ready for your review.
- Pending, missing, skipped, neutral, or unknown checks keep the PR draft.
  Unresolved checks reaching the task deadline produce needs-attention.
- Failing checks or high-severity findings need your attention. The PR stays
  draft; no remote CI or Review repair loop starts.

Ready means the configured delivery conditions were met. It does not mean you
accepted the change or that the bot may merge it. Read the diff, tests, and
findings before merging. Outstanding bot PRs count toward the configured cap so
the worker cannot create an unlimited review backlog.

Result reporting is deduplicated across passes; ordinary progress stays in
status and logs. A failed result requires a decision, not repeated notifications.
Record your review and intervention time in the [trial worksheet](background-trial.md).

## Boundaries

The pilot supports explicit GitHub issue delegation through one persistent
worker. It does not discover its own tasks, coordinate workers across hosts,
implement GitLab background intake, repair remote CI, merge, or deploy. Provider
charges and missing usage reports are not a reliable hard dollar budget; the
enforced limits are runtime, repair, concurrency, change limits, and open PRs.

See [Approval policy](approval-policy.md#delegated-background-tasks) for the
separate authorization contract and [Architecture](architecture.md#background-coordination)
for implementation ownership. Return to the [documentation index](README.md).

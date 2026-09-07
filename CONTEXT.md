# AgentMachinist Context

AgentMachinist coordinates a human-approved path from a Task to a reviewed local
implementation while keeping Harness execution isolated from the developer's
checkout. GitHub and GitLab intake and publication are optional.

## Language

**Task**:
A bounded development objective owned by the controller. Local Tasks have IDs
such as `T1`; an imported GitHub/GitLab issue is optional provenance. Existing
GitHub issue Tasks retain numeric selectors and their Spec PR. One Task has one
current Task Run per Phase, with previous attempts retained.
_Avoid_: Job, ticket, work item

**Phase**:
One of the ordered kinds of machine work: Spec, Execute, or Review. Approval is a
human Gate between Spec and Execute; human review and integration remain the
final Gate after the machine Review Phase. Publication is a separate controller
operation, not a Phase.
_Avoid_: Stage, step

**Spec**:
The Markdown implementation contract committed to the Task branch. A Spec is identified by the exact Git commit that contains it.
_Avoid_: Plan, proposal

**Approval**:
A human decision authorizing one exact Spec commit for Execute. Local Approval
binds repository, Task, Spec SHA, actor, and time. The legacy GitHub flow requires
its trusted workflow marker and label. Revising the Spec invalidates Approval.
_Avoid_: Review approval, permission

**Gate**:
A control transfer that requires durable Evidence before work can proceed.
Gate 1 is Approval; the final human Gate is review and explicit local integration
or a human merge on the forge. Verification Gates run the configured checks.
_Avoid_: Checkpoint

**Task Run**:
The durable local record of one Phase attempt for a Task, including claim, result, evidence, and failure state.
_Avoid_: Session, execution

**Claim**:
Exclusive local ownership of a Task Phase while it is running. A Claim prevents
local workers using the same runtime from spending Harness time on the same
Task. It does not coordinate different runner checkouts or machines.
_Avoid_: Lock, reservation

**Workshop**:
The isolated worktree or clone where a harness reads or changes repository files for a Task.
_Avoid_: Workspace, checkout

**Harness**:
A coding-agent CLI selected by configuration. Claude Code, OpenCode, PI, and Codex are Harness adapters.
_Avoid_: Agent provider, model

**Evidence**:
Durable facts produced by a Task Run: approved Spec commit, verification result,
implementation commit, independent Review report, and error details. Local
integration and optional PR/MR publication also retain recoverable intent and
observed results.
_Avoid_: Log, output

## Flagged ambiguities

- "Approve" in a forge review UI does not authorize local Execute. AgentMachinist
  Approval names an exact Spec through the local CLI or the legacy GitHub
  workflow. GitLab remote reviews are not local Approval.
- The existing code uses `Workspace` for the Workshop module. Keep the public code name for compatibility; documentation uses Workshop for the domain concept.

## Example dialogue

**Developer:** "Why did Execute stop after I fixed the Spec?"

**AgentMachinist:** "The Approval names the earlier Spec commit, so Gate 1 is stale. Approve the new Task branch head, then retry the Execute Phase. The failed Task Run and Workshop remain available as Evidence."

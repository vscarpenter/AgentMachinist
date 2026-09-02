# AgentMachinist Context

AgentMachinist coordinates a human-approved path from a GitHub issue to a reviewable implementation while keeping task execution isolated from the developer's checkout.

## Language

**Task**:
A GitHub issue selected for AgentMachinist. One Task has at most one active Spec PR and one current Task Run per Phase.
_Avoid_: Job, ticket, work item

**Phase**:
One of the ordered kinds of machine work: Spec, Execute, or Review. Approval is a human Gate between Spec and Execute; human review and merge remain the final Gate after the machine Review Phase.
_Avoid_: Stage, step

**Spec**:
The Markdown implementation contract committed to the Task branch. A Spec is identified by the exact Git commit that contains it.
_Avoid_: Plan, proposal

**Approval**:
A human decision authorizing one exact Spec commit for Execute. An Approval becomes stale when the Task branch head changes.
_Avoid_: Review approval, permission

**Gate**:
A control transfer that requires durable evidence before the next Phase can begin. Gate 1 is Approval; Gate 2 is human review and merge.
_Avoid_: Checkpoint

**Task Run**:
The durable local record of one Phase attempt for a Task, including claim, result, evidence, and failure state.
_Avoid_: Session, execution

**Claim**:
Exclusive local ownership of a Task Phase while it is running. A Claim prevents two local watchers from spending harness time on the same Task.
_Avoid_: Lock, reservation

**Workshop**:
The isolated worktree or clone where a harness reads or changes repository files for a Task.
_Avoid_: Workspace, checkout

**Harness**:
A coding-agent CLI selected by configuration. Claude Code, OpenCode, PI, and Codex are Harness adapters.
_Avoid_: Agent provider, model

**Evidence**:
Durable facts produced by a Task Run: approved Spec commit, verification result, implementation commit, independent Review report, PR, and error details.
_Avoid_: Log, output

## Flagged ambiguities

- "Approve" in GitHub means a pull-request review action. AgentMachinist Approval means authorizing an exact Spec commit through its label/comment/CLI flow.
- The existing code uses `Workspace` for the Workshop module. Keep the public code name for compatibility; documentation uses Workshop for the domain concept.

## Example dialogue

**Developer:** "Why did Execute stop after I fixed the Spec?"

**AgentMachinist:** "The Approval names the earlier Spec commit, so Gate 1 is stale. Approve the new Task branch head, then retry the Execute Phase. The failed Task Run and Workshop remain available as Evidence."

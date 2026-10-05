# AgentMachinist documentation

AgentMachinist helps your coding agent make a change you can check before
accepting it. Start with a free rehearsal, then try one small change in your
own project. The other pages answer specific questions when you need them.

| I want to… | Read this |
| --- | --- |
| Understand the workflow | [How it works](how-it-works.html): one diagram showing what you decide and what the controller does. |
| Try it, then complete my first Task | [Start here](tldr.md): free guided rehearsal, project readiness, plan Approval, inspection, and local integration. |

Prefer illustrated instructions? The [visual first-run guide](first-run-guide.html)
walks through the same journey with examples and recovery help.

This is the current operating documentation for **AgentMachinist 0.19.0**.
These onboarding additions are unreleased; use this checkout until a package
release includes them. See the [changelog](https://github.com/vscarpenter/AgentMachinist/blob/main/CHANGELOG.md)
for details.

## Find a specific answer

| Question | Reference |
| --- | --- |
| How do local settings, context, amendments, and publication work? | [Local workflow](local-workflow.md) |
| How do I configure Gates, repair, profiles, notifications, or GitHub automation? | [Configuration and GitHub reference](getting-started.md) |
| How do I diagnose, retry, cancel, monitor, or release? | [Operator runbook](operator-runbook.md) |
| Which Harness can I use, and how do I authenticate or extend it? | [Harness support](harnesses.md) |
| What does my Approval authorize? | [Approval policy](approval-policy.md) |
| What is enforced, and what risks remain? | [Trust model](trust-model.md) |
| How is the controller implemented? | [Architecture and lifecycle](architecture.md) |

The long `getting-started.md` file remains at its existing URL because generated
configuration and external links use it. Treat it as a reference; begin with
[Start here](tldr.md) for a first Task.

## Other ways to learn

These cover the same workflow in different formats; none is a prerequisite.

| Format | Resource |
| --- | --- |
| Interactive tour and web directory | [Documentation home](index.html) |
| 90-second video | [AgentMachinist in 90 seconds](https://www.youtube.com/watch?v=XfLDEiakdkw) on YouTube |

## Architecture decisions

These explain why the implementation has its current boundaries. Use the
references above for commands and current behavior.

- [ADR 0001: Review, plugins, and telemetry](adr/0001-review-plugin-telemetry-boundaries.md)
- [ADR 0002: Controller module ownership](adr/0002-deep-module-ownership.md)
- [ADR 0003: Local workflow and optional publication](adr/0003-local-workflow-and-optional-publication.md)
- [ADR 0004: Local readiness and diagnostics](adr/0004-local-readiness-and-diagnostic-boundaries.md)

## Historical design records

Retained for provenance, not as setup instructions. Old checklists, version
numbers, and worker instructions describe the work at the time.

| Date | Record |
| --- | --- |
| 2026-08-16 | [Original design specification](superpowers/specs/2026-08-16-agentmachinist-design.md) |
| 2026-08-17 | [Reliability and usability specification](superpowers/specs/2026-08-17-reliability-and-usability-hardening.md) |
| 2026-08-17 | [Build-system hardening plan](superpowers/plans/2026-08-17-build-system-hardening.md) |
| 2026-09-03 | [Resume/push and Approval specification](superpowers/specs/2026-09-03-resume-push-and-approve-flags.md) |
| 2026-09-03 | [Resume/push and Approval plan](superpowers/plans/2026-09-03-resume-push-and-approve-flags.md) |
| 2026-09-03 | [Spec-to-Execute simplification plan](superpowers/plans/2026-09-03-spec-to-execute-simplification.md) |
| 2026-09-07 | [Local workflow and GitLab plan](superpowers/plans/2026-09-07-local-workflow-gitlab.md) |

The [old onboarding URL](onboarding.html) redirects to the visual first-run guide.
The retired [job card](job-card.html) and [animated explainer](explainer.html)
URLs redirect to the first-run guide and [How it works](how-it-works.html).
[CNAME](CNAME) and [.nojekyll](.nojekyll) configure the static site; they are not
guides. Editing these sources does not publish the website or a package release.

## Keeping the documentation small

The diagram owns the short explanation; Start here owns the first-Task path.
Detailed settings belong in the configuration reference, operational recovery in
the runbook, and implementation decisions in architecture/ADRs. Add detail there
and link to it instead of creating another introduction. Both this index and the
[web directory](index.html#documentation) list every guide and historical record.

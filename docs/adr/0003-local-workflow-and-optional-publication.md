# ADR 0003: Local workflow and optional publication

Date: 2026-09-07

Status: Accepted

Decider: Vinny Carpenter through approval of the reviewed implementation roadmap.

## Context

GitHub supplies Task identity, Approval and delivery even for local computation.
Solo adoption needs a local result before forge setup; teams need optional GitHub
or GitLab collaboration without repeating machine work after a publication error.

## Decision

Add controller-owned local Tasks beside compatible legacy GitHub Tasks. Reuse
claimed Phase dispatch, durable history, custody, exact-Spec Approval, verification
and independent Review. Give local Tasks distinct IDs and runtime storage. Treat
external issue identity as provenance and publication as separate recovery work.

Local Approval is an explicit human controller action. Legacy GitHub Approval
still requires its trusted workflow issuer. GitLab uses explicitly bound glab
host/project operations, never fake GitHub comments or approvals.

Permit explicit human-directed local fast-forward integration after validating
base and reviewed candidate. This narrowly supersedes the never-merges wording
in ADRs 0001/0002: automatic and remote merges stay outside the product. The final
human Gate remains mandatory.

## Consequences

- First useful Task requires no origin, gh/glab, labels or hosted workflows.
- Existing CLI, configuration and history remain compatible.
- Publication failure does not repeat paid work or invalidate local Evidence.
- Local records are not distributed ownership or OS isolation.
- GitLab imports issues and publishes MRs; hosted Spec CI is not included.
- Offline inference requires separately provisioned and verified local models.

## Alternatives rejected

Fake local PRs/comments preserve platform complexity. Replacing legacy GitHub
behavior immediately adds migration risk. A new database/broker or broad plugin
framework is unnecessary for one runner and two actual adapters. Automatic
integration would weaken the human decision.

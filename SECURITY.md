# Security Policy

## Reporting a Vulnerability

Please do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/vscarpenter/AgentMachinist/security/advisories/new)
so the report and any reproduction details remain private.

Include the affected version, impact, prerequisites, reproduction steps, and any
suggested mitigation. The maintainer will aim to acknowledge a report within
five business days and coordinate remediation and disclosure with the reporter.
AgentMachinist does not currently operate a paid bug-bounty program.

## Supported Versions

The latest PyPI release and the current `main` branch receive security fixes.
Older releases are not guaranteed to receive backports.

## System and Scope

AgentMachinist is a local Python controller for GitHub issues, Git workspaces,
coding harnesses, tests, and pull-request transitions. Reports may cover the
published Python package, controller source, bundled prompts and workflows, and
release automation maintained in this repository.

Issue bodies and pull-request branches are untrusted input. The repository
owner, default branch, local configuration and test command, installed harness,
and the local user account launching AgentMachinist are trusted inputs.

## Security Invariants

Security-sensitive behavior should preserve these properties:

- Implementation requires approval evidence bound to the current spec PR SHA.
- `pull_request_target` automation must not check out or execute untrusted PR
  head code.
- Harness subprocesses must not receive controller GitHub or SSH-agent
  credentials that AgentMachinist claims to remove.
- Concurrent remote changes must not be overwritten; pushes remain lease-bound.
- Untrusted issue and repository data must not escape configured workspace,
  command-construction, or size boundaries.
- Logs and errors must not disclose credentials or other avoidable secrets.
- AgentMachinist never merges a pull request.

## Reportable Findings

Please report realistic ways to bypass approval, cross trust boundaries, expose
credentials, execute attacker-controlled input unexpectedly, overwrite remote
work, escape workspace boundaries, or compromise the release pipeline. Include
the reachable impact and assumptions needed to reproduce the issue.

## Known Limitations

AgentMachinist is not an operating-system sandbox. Harnesses, repository tests,
hooks, and provider plugins execute with access available to the launching user.
Local task claims are not distributed locks, and model output can be incorrect
even when tests pass. See `docs/trust-model.md` for the complete operational
boundary and recommended isolation.

## Out of Scope

General support requests, model-quality complaints without security impact,
denial-of-service reports that only exhaust the reporter's own local resources,
and vulnerabilities solely in third-party services should use the appropriate
public issue or upstream reporting channel instead.

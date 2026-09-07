# Approved readiness and hardening implementation plan

1. Extract read-only local configuration resolution shared with setup. Add local
   doctor with accumulated checks, preserving legacy report rendering and JSON.
2. Add CLI contract tests, connect `doctor --local`, and verify no-write/no-forge
   behavior in disposable repositories. Keep verification execution opt-in.
3. Add real-Git stale/deleted-base regressions and targeted remote-base handling;
   preserve exact remote Task recovery, SHA pinning, and local-only provisioning.
4. Add shared bounded diagnostic sanitization with synthetic-secret tests; wire
   Git/forge/doctor errors without exposing raw credential-bearing command text.
5. Update active guides, ADR, changelog, and module map. Review independently.
6. Run canonical verification and installed/local CLI smoke checks, record limits,
   and commit the complete implementation in coherent local commits.

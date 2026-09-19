# Background pilot implementation plan

1. Record the approved spec and plan on `codex/background-pilot`.
2. Add failing contracts and implement a distinct delegation authorization shared
   by local coordination, Phases, Evidence validation, and publication.
3. Add failing contracts for container execution and trusted GitHub queue/CI
   observation; implement these as injected adapters.
4. Add the durable single-worker coordinator, opt-in configuration, intake
   idempotency, shared deadline, bounded admission, cancellation, explicit retry,
   publication reconciliation, and sparse result reporting.
5. Wire CLI readiness/run/status/cancel/retry, configure one-shot operation,
   document worker provisioning, and add a concise trial worksheet.
6. Prove the controlled complete workflow, run affected tests and canonical gates,
   obtain an independent review, fix findings, and commit coherent increments.
7. Prepare the real-project pilot selected by the user; report any external
   runtime, authentication, or target prerequisites separately from code completion.

No extra spec or plan approval gate is required; the user approved this design.

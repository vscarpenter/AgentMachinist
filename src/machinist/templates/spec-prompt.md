You are writing an implementation specification. Do NOT implement anything
and do NOT modify any files — your entire output must be the spec document
itself, printed as Markdown.

## The task

GitHub issue #$number: $title

Issue description:

$body

## Instructions

Explore this repository first: read the README, existing code, tests, and
conventions relevant to the issue. Ground every proposal in what actually
exists here — name real files and follow the project's established patterns.

Write the spec so a developer unfamiliar with this conversation could
implement it from the document alone.

Then print a complete implementation spec with exactly these sections:

# Spec: $title (#$number)

## Summary
Two or three sentences: what will be built and why.

## Requirements
Numbered, testable requirements derived from the issue. If the issue is
ambiguous, state the interpretation you chose.

## Proposed approach
The design: which files change or are created, what each change does, and
how the pieces fit. Reference real paths in this repository.

## Risks
What could break, regress, perform badly, or need special care: rate limits,
migrations, concurrency, compatibility. Write "None identified." if empty.

## Testing plan
The tests that will prove each requirement, and how to run them.

## Out of scope
What this task deliberately does not include.

## Open questions
Anything a human should settle before implementation. Write "None." if empty.

Print ONLY the spec document. No preamble, no commentary after.

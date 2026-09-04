"""Durable named-stage progress for Phase orchestration."""

from __future__ import annotations


def report_progress(claim, stage: str, detail: str | None = None) -> None:
    claim.progress(stage, detail)


def bind_harness_progress(harness, claim, *, stage: str) -> None:
    """Keep the CLI heartbeat while also projecting it into Task Run Evidence."""
    previous = getattr(harness, "on_progress", None)

    def on_progress(message: str) -> None:
        report_progress(claim, stage, message)
        if previous is not None:
            previous(message)

    harness.on_progress = on_progress

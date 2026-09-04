"""Best-effort Workshop cleanup after a Phase outcome is already known."""

from __future__ import annotations

from pathlib import Path


def finish_workshop_cleanup(
    workspace,
    path: Path,
    *,
    success: bool,
    claim=None,
) -> str | None:
    """Clean a Workshop without rewriting the Phase outcome on failure.

    Delivery and cleanup are separate facts. Once a PR has been observed on
    GitHub, inability to remove its local Workshop is recovery Evidence, not a
    failed delivery. The same helper also preserves an earlier Phase exception
    when failure cleanup has trouble.
    """
    try:
        workspace.cleanup(path, success=success)
    except Exception as exc:  # noqa: BLE001 - cleanup is explicitly best effort
        outcome = "successful delivery" if success else "Phase failure"
        warning = (
            f"Workshop cleanup failed after {outcome}; retained {path}: "
            f"{type(exc).__name__}: {str(exc).strip() or type(exc).__name__}"
        )
        if claim is not None:
            try:
                claim.checkpoint(
                    cleanup_succeeded=False,
                    cleanup_warning=warning,
                    retained_workspace_path=str(path),
                )
            except Exception as checkpoint_error:  # noqa: BLE001
                warning += (
                    "; cleanup warning could not be checkpointed: "
                    f"{type(checkpoint_error).__name__}: {checkpoint_error}"
                )
        return warning
    return None

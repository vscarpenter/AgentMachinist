"""Preserve task intake before validation or a remote write can fail."""

from pathlib import Path
from uuid import uuid4

from machinist.runtime_paths import RuntimeDirectory, atomic_write_text_file


def save_task_draft(repo_root: Path, body: str) -> Path:
    runtime = RuntimeDirectory.bind(
        repo_root / ".machinist/runs/intake", repo_root=repo_root
    )
    runtime.ensure(create=True)
    path = runtime.path / f"draft-{uuid4().hex}.md"
    atomic_write_text_file(path, body)
    return path

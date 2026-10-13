"""Shared helpers for recorder tests (real files in temporary directories)."""

from __future__ import annotations

import os
from pathlib import Path

from agentbodycam.core.store import EventStore
from agentbodycam.models.events import Event, EventType

FILE_TYPES = {EventType.FILE_CREATED, EventType.FILE_MODIFIED, EventType.FILE_DELETED}


def write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def bump_mtime(path: Path, seconds: int = 5) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + seconds * 1_000_000_000))


def file_events(events: list[Event]) -> list[tuple[str, str | None]]:
    return [(e.type.value, e.path) for e in events if e.type in FILE_TYPES]


def all_events(store: EventStore, session_id: str) -> list[Event]:
    return store.read_events(session_id)


def store_bytes(project: Path) -> bytes:
    """Every byte AgentBodycam wrote under .agentbodycam/, concatenated."""
    root = project / ".agentbodycam"
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def try_symlink(link: Path, target: Path, *, directory: bool = False) -> bool:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        return False
    return True

"""Append-only JSONL and SQLite audit logging, plus Unix process locking."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - project targets macOS/Linux, retained for importability elsewhere.
    fcntl = None  # type: ignore[assignment]


class LockUnavailable(RuntimeError):
    pass


@contextmanager
def database_lock(database_path: Path | str) -> Iterator[None]:
    """Prevent concurrent mutating CLI operations on macOS/Linux using an advisory lock."""
    lock_path = Path(str(Path(database_path).absolute()) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is None:
            raise LockUnavailable("This platform lacks fcntl locking; refusing an unlocked mutating operation")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockUnavailable(f"Another academic-library operation holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_audit(log_directory: Path | str, event: dict[str, Any]) -> Path:
    log_dir = Path(log_directory)
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = log_dir / f"audit-{now:%Y-%m}.jsonl"
    record = {"timestamp": now.isoformat(timespec="seconds"), **event}
    # O_APPEND makes each short line append-only at the application interface.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o640)
    try:
        payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return path

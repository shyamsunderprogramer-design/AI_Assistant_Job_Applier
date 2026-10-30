"""One change at a time to a small file the app keeps -- in this process and in any other -- and each
written whole: a private temporary file beside it, renamed over it. A reader never sees half a file, and
two writers never share a temporary file (29 Oct 2026: the answer bank was rewritten in place with no
lock, so a save read during another could find it empty and write back only its own answer)."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextmanager
def one_writer(path: Path):
    """Hold while reading the file, changing it and writing it back."""
    path = Path(path)
    with _locks_guard:
        lock = _locks.setdefault(str(path.resolve()), threading.Lock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + ".lock"), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def write_whole(path: Path, text: str) -> None:
    """The file's new contents, published in one step, keeping its permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if path.exists():
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

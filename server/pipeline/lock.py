"""Cross-platform single-instance advisory lock.

The lock must be PER INSTANCE and owned by whoever runs the pipeline: a
shared lock made two instances scheduled at the same minute collide (the
second silently exited), and a lock left behind by another user is
unopenable, which looks identical to "already running". Non-blocking by
design — a skipped run is safe, since harvesting and drafting are both
idempotent and the next scheduled run (cron on a server, the in-process
scheduler on a laptop) picks up anything new.

POSIX uses fcntl.flock; Windows has no flock, so msvcrt.locking is used
instead — same non-blocking, whole-file, advisory semantics.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import IO, Iterator

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fh: IO) -> bool:
        # msvcrt.locking needs at least one byte to lock; a fresh lock file is empty.
        fh.seek(0, 2)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fh: IO) -> None:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fh: IO) -> bool:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh: IO) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def nightly_lock(lock_path: Path) -> Iterator[bool]:
    """Yields True if the lock was acquired, False if another run holds it.

        with nightly_lock(path) as acquired:
            if not acquired:
                return  # another run is in progress; skip
            ...
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        acquired = _try_lock(fh)
        try:
            yield acquired
        finally:
            if acquired:
                _unlock(fh)

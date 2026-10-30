"""One process per data directory, enforced with an OS file lock.

Two processes writing the same log could sign two different entries with the
same seq: a fork of your own identity. So before anything opens the key files
or (from Phase 2) the database, it takes an exclusive lock on
`<data-dir>/hearth.lock`. The OS releases the lock automatically if the process
dies, so there are no stale lock files to clean up.

- POSIX: `fcntl.flock(LOCK_EX | LOCK_NB)` on the whole file.
- Windows: `msvcrt.locking(LK_NBLCK)` on the file's first byte.

Joke: like a bathroom door lock. One occupant, no exceptions, and nobody
appreciates you trying the handle repeatedly.
"""

import os
import sys
from pathlib import Path
from types import TracebackType

from hearth import config

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LockHeldError(Exception):
    """Another process already holds the lock for this data directory."""


class DataDirLock:
    """An exclusive lock on one data directory. Use as a context manager or acquire()/release()."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / config.LOCK_FILE
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Take the lock right now, or raise LockHeldError. Never waits."""
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise LockHeldError(
                f"{self.path.parent} is already in use by another Hearth process. "
                "One node per data directory, please."
            ) from error
        self._descriptor = descriptor

    def release(self) -> None:
        """Give the lock back. Safe to call when not holding it."""
        if self._descriptor is None:
            return
        try:
            _unlock(self._descriptor)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> "DataDirLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _lock(descriptor: int) -> None:
    if sys.platform == "win32":
        # msvcrt locks a byte range from the current position; lock byte 0.
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(descriptor: int) -> None:
    if sys.platform == "win32":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)

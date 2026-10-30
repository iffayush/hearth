"""Tests for lockfile.py: only one process may use a data directory at a time."""

import subprocess
import sys
from pathlib import Path

import pytest

from hearth.lockfile import DataDirLock, LockHeldError

# A child process that takes the lock, says so, and holds it until stdin closes.
HOLD_LOCK_SCRIPT = """
import sys
from pathlib import Path
from hearth.lockfile import DataDirLock
with DataDirLock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"""


def test_second_lock_in_same_process_is_refused(tmp_path: Path) -> None:
    with DataDirLock(tmp_path):
        with pytest.raises(LockHeldError, match="already in use"):
            DataDirLock(tmp_path).acquire()


def test_lock_can_be_taken_again_after_release(tmp_path: Path) -> None:
    first = DataDirLock(tmp_path)
    first.acquire()
    first.release()
    with DataDirLock(tmp_path):
        pass


def test_release_twice_is_harmless(tmp_path: Path) -> None:
    lock = DataDirLock(tmp_path)
    lock.acquire()
    lock.release()
    lock.release()


def test_second_process_on_same_data_dir_is_refused(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK_SCRIPT, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        # readline() blocks until the child really holds the lock: no sleeps.
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(LockHeldError):
            DataDirLock(tmp_path).acquire()
    finally:
        holder.communicate(input="", timeout=10)
    # Once the holder exits, the OS frees the lock.
    with DataDirLock(tmp_path):
        pass


def test_different_data_dirs_dont_conflict(tmp_path: Path) -> None:
    (tmp_path / "alice").mkdir()
    (tmp_path / "bob").mkdir()
    with DataDirLock(tmp_path / "alice"), DataDirLock(tmp_path / "bob"):
        pass

"""Backups: a consistent copy of a node's data directory in one .tar file.

`python -m hearth backup --out FILE` works whether or not the node is running:

1. **The database** is copied with SQLite's online backup API
   (`Store.snapshot()`) by whichever process holds the data-dir lock: the
   running node (asked over the control channel) or the CLI itself.
   `make_snapshot()` puts that copy in the data dir as `backup-<random>.db`.
   Copying `hearth.db` as a plain file while the node runs could catch it
   mid-write and give you a torn copy.
2. **The archive** is then written by the CLI (`write_archive()`): the
   snapshot as `hearth.db`, plus `identity.key`, `group.key`, `config.json`
   and every chunk file. Those are safe to copy as plain files: key files only
   change while no node runs, and each chunk is written once, atomically,
   before the database row that mentions it, so every chunk the snapshot
   lists is already on disk.

The archive holds only the files named in this module, never "the whole
folder", so nothing else can sneak in: not the passphrase file (even if
someone keeps it in the data dir), not the control token, the lock or the WAL
files. Its layout is the data dir's own, so restoring is
`tar -xf FILE -C <empty data dir>`. The CLI writes it, not the node, so it
lands wherever the person running `backup` can write, even though systemd
keeps the node itself away from most of the disk.

Joke: a snapshot of the fire, minus the matches. Keep the matches (your
passphrase) in a different drawer.
"""

import os
import re
import tarfile
import tempfile
from pathlib import Path

from hearth import config
from hearth.store import Store

# Copied as they are, when present (group.key arrives with create-group or join).
PLAIN_FILES = (config.IDENTITY_FILE, config.GROUP_KEY_FILE, config.SETTINGS_FILE)
# mkstemp's random part is lowercase letters, digits and underscores.
SNAPSHOT_PATTERN = re.compile(r"backup-[a-z0-9_]{1,32}\.db")
CHUNK_DIR_PATTERN = re.compile(r"[0-9a-f]{2}")
CHUNK_FILE_PATTERN = re.compile(r"[0-9a-f]{64}")


class BackupError(Exception):
    """The backup can't be written. The message says why."""


def make_snapshot(store: Store, data_dir: Path) -> str:
    """Snapshot the database into a new file in `data_dir` and return the file's name."""
    # mkstemp: a fresh name nobody else has open, created with mode 0600.
    handle, name = tempfile.mkstemp(prefix="backup-", suffix=".db", dir=data_dir)
    os.close(handle)
    path = Path(name)
    try:
        store.snapshot(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path.name


def is_snapshot_name(name: object) -> bool:
    """True for a name make_snapshot() could have returned (and nothing with a path in it)."""
    return isinstance(name, str) and SNAPSHOT_PATTERN.fullmatch(name) is not None


def write_archive(data_dir: Path, snapshot: Path, out: Path) -> dict[str, int]:
    """Write the backup to `out`: the snapshot as hearth.db, the key files, config.json, chunks.

    Returns how many chunks went in and the archive's size in bytes.
    """
    if out.exists():
        raise BackupError(f"{out} already exists; pick a new name (say, with today's date in it)")
    partial = out.with_name(out.name + ".partial")
    handle = open_private(partial)
    try:
        with os.fdopen(handle, "wb") as file:
            with tarfile.open(fileobj=file, mode="w") as archive:
                archive.add(snapshot, arcname=config.DATABASE_FILE)
                for name in PLAIN_FILES:
                    if (data_dir / name).is_file():
                        archive.add(data_dir / name, arcname=name)
                chunks = add_chunks(archive, data_dir / config.CHUNKS_DIR)
            file.flush()
            os.fsync(file.fileno())
        # Only a finished archive ever gets the real name.
        os.replace(partial, out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {"chunks": chunks, "bytes": out.stat().st_size}


def open_private(path: Path) -> int:
    """Create `path` for writing, mode 0600 from the start: it will hold the (encrypted) keys."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        return os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise BackupError(f"{path} exists, probably from an interrupted backup; delete it and retry") from error
    except OSError as error:
        raise BackupError(f"can't write {path}: {error.strerror or error}") from error


def add_chunks(archive: tarfile.TarFile, chunks_dir: Path) -> int:
    """Add chunks/ and every chunk file in it (only names shaped like chunks). Returns the count."""
    if not chunks_dir.is_dir():
        return 0
    archive.add(chunks_dir, arcname=config.CHUNKS_DIR, recursive=False)
    count = 0
    for subdir in sorted(chunks_dir.iterdir()):
        if not (subdir.is_dir() and CHUNK_DIR_PATTERN.fullmatch(subdir.name)):
            continue
        archive.add(subdir, arcname=f"{config.CHUNKS_DIR}/{subdir.name}", recursive=False)
        for chunk in sorted(subdir.iterdir()):
            # ChunkStore only ever writes plain files at chunks/<first 2 hex>/<64 hex>.
            if (chunk.is_file() and not chunk.is_symlink() and CHUNK_FILE_PATTERN.fullmatch(chunk.name)
                    and chunk.name.startswith(subdir.name)):
                archive.add(chunk, arcname=f"{config.CHUNKS_DIR}/{subdir.name}/{chunk.name}")
                count += 1
    return count

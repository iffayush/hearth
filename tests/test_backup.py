"""Tests for backup.py and the `backup` command: consistent snapshots, an archive
of exactly the right files (never the passphrase), and a restore that verifies
and runs.

Joke: a backup you've never restored is a rumour. These tests check the rumour.
"""

import asyncio
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from hearth import actions, audit, backup, config, crypto
from hearth.actions import ActionError, Context
from hearth.chunks import ChunkStore
from hearth.store import Store

from conftest import make_identity, make_image, post_text
from test_cli import keyed, run_hearth
from test_cli_run import running_home, start_run, wait_for_log_line
from test_node import free_port

PASSPHRASE = "correct horse battery staple"


@pytest.fixture
def home(tmp_path: Path) -> Iterator[Context]:
    """A home node's data dir with a group, a text post and an image post (no node running)."""
    identity, data_dir = make_identity(tmp_path, "home", PASSPHRASE)
    (data_dir / config.CHUNKS_DIR).mkdir()
    store = Store.open(data_dir / config.DATABASE_FILE)
    context = Context(store, ChunkStore(data_dir / config.CHUNKS_DIR, store), identity, crypto.new_group_key(),
                      is_home=True, data_dir=data_dir)
    actions.create_group(store, identity)
    post_text(store, identity, context.group_key, "the kettle is on")
    picture = make_image(tmp_path, "cat.png", 2 * config.CHUNK_BYTES + 10)
    actions.post_with_images(context, "my cat", [("cat.png", picture.read_bytes())])
    (data_dir / config.GROUP_KEY_FILE).write_bytes(b"pretend group key file")
    (data_dir / config.SETTINGS_FILE).write_text("{}", encoding="utf-8")
    yield context
    store.close()


def archive_names(path: Path) -> set[str]:
    with tarfile.open(path) as archive:
        return set(archive.getnames())


def chunk_names(context: Context) -> set[str]:
    return {f"chunks/{chunk_id[:2]}/{chunk_id}" for chunk_id in context.chunks.ids()} | {
        f"chunks/{chunk_id[:2]}" for chunk_id in context.chunks.ids()}


def test_snapshot_is_a_complete_working_copy(home: Context) -> None:
    assert home.data_dir is not None
    name = backup.make_snapshot(home.store, home.data_dir)
    assert backup.is_snapshot_name(name)
    with Store.open(home.data_dir / name) as copy:
        assert copy.entry_count() == home.store.entry_count() == 3
        assert audit.verify(copy) == {"checked": 3, "problems": []}


def test_the_archive_holds_exactly_the_right_files(home: Context, tmp_path: Path) -> None:
    """Only the allowlist goes in: never the passphrase, wherever someone keeps it."""
    data_dir = home.data_dir
    assert data_dir is not None
    (data_dir / "passphrase").write_text(PASSPHRASE, encoding="utf-8")
    for junk in ("control.token", "hearth.lock", "hearth.db-wal", "notes.txt"):
        (data_dir / junk).write_text("not for the backup", encoding="utf-8")
    (data_dir / "chunks" / "zz").mkdir()
    (data_dir / "chunks" / "zz" / "stray").write_text(PASSPHRASE, encoding="utf-8")
    some_dir = next(path for path in (data_dir / "chunks").iterdir() if path.name != "zz")
    (some_dir / ("cd" * 32)).write_text(PASSPHRASE, encoding="utf-8")  # chunk-shaped, wrong folder

    out = tmp_path / "backup.tar"
    snapshot = data_dir / backup.make_snapshot(home.store, data_dir)
    written = backup.write_archive(data_dir, snapshot, out)

    assert written["chunks"] == 3
    expected = {"hearth.db", "identity.key", "group.key", "config.json", "chunks"} | chunk_names(home)
    assert archive_names(out) == expected
    assert PASSPHRASE.encode("utf-8") not in out.read_bytes()
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600


def test_an_existing_file_is_never_overwritten(home: Context, tmp_path: Path) -> None:
    assert home.data_dir is not None
    out = tmp_path / "backup.tar"
    out.write_text("last night's backup", encoding="utf-8")
    snapshot = home.data_dir / backup.make_snapshot(home.store, home.data_dir)
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.write_archive(home.data_dir, snapshot, out)
    assert out.read_text(encoding="utf-8") == "last night's backup"


def test_a_failed_backup_leaves_no_half_written_archive(home: Context, tmp_path: Path) -> None:
    assert home.data_dir is not None
    out = tmp_path / "backup.tar"
    with pytest.raises(FileNotFoundError):
        backup.write_archive(home.data_dir, home.data_dir / "backup-gone.db", out)
    assert list(tmp_path.glob("backup.tar*")) == []


@pytest.mark.parametrize("name", ["backup-ab12_x.db", "backup-q.db"])
def test_snapshot_names_are_recognised(name: str) -> None:
    assert backup.is_snapshot_name(name)


@pytest.mark.parametrize("name", ["hearth.db", "../backup-x.db", "backup-x.db/../../etc", "backup-.db", "backup-X.db", 7])
def test_anything_else_is_not_a_snapshot_name(name: object) -> None:
    assert not backup.is_snapshot_name(name)


async def test_backup_needs_a_data_dir(home: Context) -> None:
    home.data_dir = None
    with pytest.raises(ActionError, match="data directory"):
        await actions.perform(home, {"op": "backup"})


# --- End to end: back up a running home node, restore, verify, run ----------------


async def test_backup_while_running_restores_cleanly(tmp_path: Path, passphrase_file: Path) -> None:
    """Phase 8's done-when, here: a backup taken while the node runs restores cleanly."""
    alice_dir, _, alice, _ = await running_home(tmp_path, passphrase_file)
    picture = make_image(tmp_path, "sunset.png", config.CHUNK_BYTES + 99)
    out = tmp_path / "nightly.tar"
    try:
        for command in (["post", "evening, all"], ["post", "look", "--image", str(picture)]):
            posted = await asyncio.to_thread(run_hearth, *keyed(command, alice_dir, passphrase_file))
            assert posted.returncode == 0 and posted.stdout.startswith("Posted as"), posted
        # The node is running, so this goes over its control channel.
        backed_up = await asyncio.to_thread(run_hearth, "backup", "--out", str(out), "--data-dir", str(alice_dir))
        assert backed_up.returncode == 0, backed_up.stderr
        assert "Backed up 3 entries and 2 image chunks" in backed_up.stdout
        assert not list(alice_dir.glob("backup-*.db")), "the snapshot should be cleaned up"
    finally:
        alice.kill()
        await alice.wait()
    assert b"hunter2" not in out.read_bytes()

    restored = tmp_path / "restored"
    restored.mkdir()
    with tarfile.open(out) as archive:
        archive.extractall(restored, filter="data")
    checked = run_hearth("debug", "verify", "--data-dir", str(restored))
    assert checked.returncode == 0, checked.stdout
    assert "All 3 entries verified" in checked.stdout and "All 2 image chunks match" in checked.stdout
    feed = run_hearth(*keyed(["feed"], restored, passphrase_file))
    assert "evening, all" in feed.stdout and "look" in feed.stdout

    # The restored copy starts as the home node, just as the original did.
    again = await start_run(restored, passphrase_file, "--listen", f"127.0.0.1:{free_port()}")
    try:
        await wait_for_log_line(again, "Listening on")
    finally:
        again.kill()
        await again.wait()

    # And verify notices if a restored chunk is damaged.
    next((restored / "chunks").rglob("?" * 64)).write_bytes(b"bit rot")
    damaged = run_hearth("debug", "verify", "--data-dir", str(restored))
    assert damaged.returncode == 1 and "BAD image chunk" in damaged.stdout

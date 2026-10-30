"""Shared test helpers.

- make_identity(): create a saved identity with the fast INTERACTIVE KDF.
- eventually(): poll an async condition instead of sleeping a fixed time.
- make_node (fixture): `await make_node("alice")` creates an identity through
  Identity.create(passphrase) (no prompts, no subprocess) and a store, starts a
  Node on a random localhost port with short timeouts, and stops it after the
  test. It's the spec's make_node(tmp_path, name, passphrase) with tmp_path
  filled in. `start=False` builds it without starting; `control=True` opens
  the control channel in its data dir (`node.data_dir` in tests).
- seed_group(admin, *members): the admin creates a group and adds each member,
  each member records the group (as `join` would), and every node gets the
  group key in memory. Nothing is synced: the members get the admin's log the
  normal way, once they connect.
- post_text(store, identity, group_key, text) / write_post(node, text): a real
  encrypted text post in that identity's own log.
- make_image(dir, name, size): a file that passes the image magic-byte check.
- fake_box(label): a well-formed but meaningless `box`, for tests below the
  encryption layer (entries, store, sync) that only need valid-looking posts.
- heads_of(node): a node's heads as plain tuples, for "everyone converged" checks.
- FAST_TIMEOUTS: the short timeouts make_node uses; tests override any of them.

Joke: conftest.py is where fixtures go to be shared, like snacks at a LAN party.
"""

import asyncio
import os
import base64
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from hearth import actions, config, crypto, feed
from hearth.chunks import ChunkStore
from hearth.entry import Entry
from hearth.identity import Identity
from hearth.node import Node
from hearth.store import Store

TEST_PASSPHRASE = "hunter2"

# Short enough that a timeout test finishes in a blink, long enough that a busy
# Windows laptop still completes a real handshake inside them.
FAST_TIMEOUTS: dict[str, float] = {
    "handshake_timeout": 0.5,
    "idle_timeout": 0.5,
    "ping_interval": 0.05,
    "heads_interval": 0.2,
    "reconnect_min_delay": 0.05,
    "reconnect_max_delay": 0.2,
}

NodeFactory = Callable[..., Awaitable[Node]]


@pytest.fixture
def passphrase_file(tmp_path: Path) -> Path:
    """A passphrase file for CLI tests (mode 0600, as POSIX requires)."""
    path = tmp_path / "passphrase.txt"
    path.write_text("hunter2\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def make_identity(tmp_path: Path, name: str, passphrase: str = TEST_PASSPHRASE) -> tuple[Identity, Path]:
    """Create `<tmp_path>/<name>/identity.key` and return the identity and its data dir."""
    data_dir = tmp_path / name
    data_dir.mkdir()
    # INTERACTIVE keeps each test under a second; MODERATE is tested once on its own.
    identity = Identity.create(data_dir / config.IDENTITY_FILE, passphrase, kdf="interactive")
    return identity, data_dir


async def eventually(condition: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> None:
    """Wait until condition() is true, failing the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"condition still false after {timeout} s")
        await asyncio.sleep(interval)


@pytest.fixture
async def make_node(tmp_path: Path) -> AsyncIterator[NodeFactory]:
    nodes: list[Node] = []

    async def factory(
        name: str, passphrase: str = TEST_PASSPHRASE, start: bool = True, control: bool = False, **overrides: Any
    ) -> Node:
        identity, data_dir = make_identity(tmp_path, name, passphrase)
        store = Store.open(data_dir / config.DATABASE_FILE)
        chunks_dir = data_dir / config.CHUNKS_DIR
        chunks_dir.mkdir()
        options: dict[str, Any] = {"listen": ("127.0.0.1", 0), **FAST_TIMEOUTS, **overrides}
        if control:
            options["control_dir"] = data_dir
        node = Node(identity, store, ChunkStore(chunks_dir, store), **options)
        node.data_dir = data_dir  # type: ignore[attr-defined]  (test convenience only)
        nodes.append(node)
        if start:
            await node.start()
        return node

    yield factory
    for node in nodes:
        await node.stop()
        node.store.close()


def seed_group(admin: Node, *members: Node) -> str:
    """Make `admin` a group's admin and every node in `members` a member. Returns the group ID."""
    group_id = actions.create_group(admin.store, admin.identity)["group"]
    admin.group_key = crypto.new_group_key()
    for member in members:
        actions.add_member(admin.store, admin.identity, member.identity.pubkey)
        actions.join_group(member.store, group_id, admin.identity.pubkey)
        member.group_key = admin.group_key
    return group_id


def post_text(store: Store, identity: Identity, group_key: bytes, text: str) -> Entry:
    """A real, encrypted, text-only post in `identity`'s own log."""
    return actions.append_own(store, identity, "post", {"box": feed.seal_post(group_key, text, [])})


def write_post(node: Node, text: str) -> Entry:
    """post_text for a node (no announcing: tests that need it call the node)."""
    assert node.group_key is not None, "seed_group first"
    return post_text(node.store, node.identity, node.group_key, text)


def make_image(directory: Path, name: str, size: int, kind: str = "png") -> Path:
    """A file that passes the magic-byte check: the right header, then random bytes."""
    headers = {"png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8\xff\xe0", "gif": b"GIF89a"}
    header = headers[kind]
    path = directory / name
    path.write_bytes(header + os.urandom(size - len(header)))
    return path


def fake_box(label: str = "") -> str:
    """A valid-looking box (base64 of at least 40 bytes) that varies with `label`."""
    return base64.b64encode(label.encode("utf-8").ljust(48, b"\0")).decode("ascii")


def heads_of(node: Node) -> dict[str, tuple[int, str]]:
    return {author: (head.seq, head.hash) for author, head in node.store.heads().items()}


@pytest.fixture
def alice() -> Identity:
    return Identity.generate()


@pytest.fixture
def bob() -> Identity:
    return Identity.generate()

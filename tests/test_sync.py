"""Tests for sync.py.

The first half drives a Syncer directly with a FakePeer, one message at a
time, so each rule can be checked exactly: partial replies, empty replies,
unsolicited ENTRIES, the pending queue, forks, bad entries, rate limits and the
reply budget. The second half uses real nodes over real sockets.
"""

import base64
from pathlib import Path
from typing import Any

import pytest

from hearth import actions, audit, canonical, config, crypto, feed, protocol
from hearth.chunks import ChunkStore
from hearth.entry import create_entry
from hearth.identity import Identity
from hearth.network import DialTarget
from hearth.session import PeerClosed
from hearth.store import Store
from hearth.sync import Syncer

from conftest import NodeFactory, eventually, fake_box, heads_of, post_text, seed_group, write_post

BIG_TEXT = "😀" * config.MAX_POST_TEXT_CHARS  # 40,000 bytes of UTF-8: a chunky entry


class FakePeer:
    """Stands in for a Peer: records what the Syncer sends."""

    def __init__(self, name: str = "fakepeer", pubkey: str | None = None) -> None:
        self.name = name
        self.peer_pubkey = pubkey
        self.sent: list[dict[str, Any]] = []

    def label(self) -> str:
        return self.name

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def last(self, kind: str) -> dict[str, Any]:
        return [message for message in self.sent if message["t"] == kind][-1]

    def count(self, kind: str) -> int:
        return sum(1 for message in self.sent if message["t"] == kind)


def wire(message: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through the real parser, exactly as a message off the network would be."""
    return protocol.parse_message(protocol.encode_message(message))


class World:
    """An admin with a group and some entries, and an empty member store ready to sync."""

    def __init__(self, tmp_path: Path) -> None:
        self.admin = Identity.generate()
        self.bob = Identity.generate()
        self.source = Store.open(tmp_path / "source.db")
        self.key = crypto.new_group_key()
        self.group = actions.create_group(self.source, self.admin)["group"]
        actions.add_member(self.source, self.admin, self.bob.pubkey)
        self.target = Store.open(tmp_path / "target.db")
        actions.join_group(self.target, self.group, self.admin.pubkey)

    def raws(self, author: str, first: int, last: int) -> list[str]:
        return [stored.raw for stored in self.source.get_range(author, first, last)]

    def heads(self, **seqs: int) -> dict[str, Any]:
        """HEADS for the given authors at the given seqs, e.g. heads(admin=2)."""
        result = {}
        for name, seq in seqs.items():
            pubkey = getattr(self, name).pubkey
            result[pubkey] = (seq, self.source.get_entry(pubkey, seq).hash)
        return wire(protocol.make_heads(result))

    def close(self) -> None:
        self.source.close()
        self.target.close()


@pytest.fixture
def world(tmp_path: Path) -> World:
    created = World(tmp_path)
    yield created
    created.close()


async def connected(world: World, **options: Any) -> tuple[Syncer, FakePeer]:
    syncer = Syncer(world.target, **options)
    peer = FakePeer()
    await syncer.peer_active(peer)  # type: ignore[arg-type]
    return syncer, peer


# --- HEADS and WANT -------------------------------------------------------------------


async def test_heads_go_out_on_connect(world: World) -> None:
    _, peer = await connected(world)
    assert peer.sent[0]["t"] == "HEADS"


async def test_peer_ahead_gets_a_want_from_our_head_plus_one(world: World) -> None:
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=2))  # type: ignore[arg-type]
    assert peer.last("WANT") == {"t": "WANT", "author": world.admin.pubkey, "from": 1, "to": 2}


async def test_want_ranges_are_capped_at_200(world: World) -> None:
    for number in range(250):
        post_text(world.source, world.admin, world.key, f"post {number}")
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=252))  # type: ignore[arg-type]
    want = peer.last("WANT")
    assert (want["from"], want["to"]) == (1, 200)


async def test_admin_is_requested_first(world: World) -> None:
    post_text(world.source, world.bob, world.key, "bob was here")
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(bob=1, admin=2))  # type: ignore[arg-type]
    wants = [message["author"] for message in peer.sent if message["t"] == "WANT"]
    # Membership lives in the admin's log, so it's fetched before anyone else's.
    assert wants == [world.admin.pubkey, world.bob.pubkey]


async def test_partial_reply_is_followed_by_a_request_for_the_rest(world: World) -> None:
    post_text(world.source, world.admin, world.key, "third")
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=3))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 1))))  # type: ignore[arg-type]
    assert world.target.head(world.admin.pubkey).seq == 1
    assert (peer.last("WANT")["from"], peer.last("WANT")["to"]) == (2, 3)


async def test_empty_reply_stops_asking_until_new_heads(world: World) -> None:
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=2))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries([])))  # type: ignore[arg-type]
    assert peer.count("WANT") == 1  # no immediate re-ask
    await syncer.handle(peer, world.heads(admin=2))  # type: ignore[arg-type]
    assert peer.count("WANT") == 2


async def test_unsolicited_entries_are_ignored(world: World) -> None:
    syncer, peer = await connected(world)
    await syncer.handle(peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 2))))  # type: ignore[arg-type]
    assert world.target.entry_count() == 0


async def test_entries_from_a_new_peer_are_announced_to_the_others(world: World) -> None:
    syncer, source_peer = await connected(world)
    bystander = FakePeer("bystander")
    await syncer.peer_active(bystander)  # type: ignore[arg-type]
    await syncer.handle(source_peer, world.heads(admin=2))  # type: ignore[arg-type]
    await syncer.handle(source_peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 2))))  # type: ignore[arg-type]
    assert world.admin.pubkey in bystander.last("HEADS")["heads"]
    # The peer it came from isn't told what it just told us.
    assert source_peer.count("HEADS") == 1


# --- Pending queue -----------------------------------------------------------------


async def test_post_before_member_add_waits_then_applies(world: World) -> None:
    bobs_post = create_entry(world.bob, world.group, None, "post", {"box": fake_box("early bird")}, 1)
    syncer, peer = await connected(world)
    await syncer.handle(peer, wire(protocol.make_heads({world.bob.pubkey: (1, bobs_post.compute_hash())})))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries([bobs_post.raw().decode()])))  # type: ignore[arg-type]
    assert len(syncer.pending) == 1 and world.target.head(world.bob.pubkey) is None

    # Bob is pending, so only the admin is requested next.
    heads = {**world.heads(admin=2)["heads"], world.bob.pubkey: [1, bobs_post.compute_hash()]}
    await syncer.handle(peer, wire({"t": "HEADS", "heads": heads}))  # type: ignore[arg-type]
    assert peer.last("WANT")["author"] == world.admin.pubkey
    await syncer.handle(peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 2))))  # type: ignore[arg-type]
    assert world.target.head(world.bob.pubkey).seq == 1
    assert len(syncer.pending) == 0


async def test_expired_pending_entries_block_their_author_for_that_peer(world: World) -> None:
    clock = [0.0]
    syncer, peer = await connected(world, clock=lambda: clock[0], pending_max_age=600)
    stranger = Identity.generate()
    post = create_entry(stranger, world.group, None, "post", {"box": fake_box("who am I")}, 1)
    await syncer.handle(peer, wire(protocol.make_heads({stranger.pubkey: (1, post.compute_hash())})))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries([post.raw().decode()])))  # type: ignore[arg-type]
    clock[0] = 601.0  # ten minutes and a second later
    await syncer.handle(peer, wire(protocol.make_heads({stranger.pubkey: (1, post.compute_hash())})))  # type: ignore[arg-type]
    assert len(syncer.pending) == 0
    assert stranger.pubkey in syncer.peers[peer].blocked  # type: ignore[index]
    assert peer.count("WANT") == 1  # never asked again


# --- Forks and bad entries ---------------------------------------------------------


async def test_heads_hash_mismatch_marks_a_fork(world: World) -> None:
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=2))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 2))))  # type: ignore[arg-type]
    lying_heads = wire(protocol.make_heads({world.admin.pubkey: (1, "00" * 32)}))
    await syncer.handle(peer, lying_heads)  # type: ignore[arg-type]
    assert world.target.forked_authors() == [world.admin.pubkey]
    assert world.admin.pubkey in syncer.peers[peer].blocked  # type: ignore[index]


async def test_fork_detected_via_heads_shows_in_status(world: World, tmp_path: Path) -> None:
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=2))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 2))))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_heads({world.admin.pubkey: (2, "00" * 32)})))  # type: ignore[arg-type]
    (tmp_path / "status-chunks").mkdir()
    context = actions.Context(world.target, ChunkStore(tmp_path / "status-chunks", world.target), None, world.key)
    assert actions.status(context)["forked"] == [world.admin.pubkey]


async def test_invalid_entry_stops_the_batch_and_blocks_the_author(world: World) -> None:
    syncer, peer = await connected(world)
    await syncer.handle(peer, world.heads(admin=2))  # type: ignore[arg-type]
    first, second = world.raws(world.admin.pubkey, 1, 2)
    tampered = second.replace(world.bob.pubkey, Identity.generate().pubkey)
    await syncer.handle(peer, wire(protocol.make_entries([first, tampered])))  # type: ignore[arg-type]
    assert world.target.head(world.admin.pubkey).seq == 1  # the good one stayed
    assert world.admin.pubkey in syncer.peers[peer].blocked  # type: ignore[index]
    assert peer.count("WANT") == 1


# --- Serving WANTs -------------------------------------------------------------------


async def test_reply_respects_the_byte_budget(world: World) -> None:
    for _ in range(60):
        post_text(world.source, world.admin, world.key, BIG_TEXT)
    syncer = Syncer(world.source)
    peer = FakePeer()
    await syncer.peer_active(peer)  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_want(world.admin.pubkey, 1, 62)))  # type: ignore[arg-type]
    reply = peer.last("ENTRIES")
    assert 1 < len(reply["entries"]) < 62
    assert sum(len(canonical.dumps(raw)) for raw in reply["entries"]) <= config.MAX_ENTRIES_REPLY_BYTES
    protocol.encode_frame(protocol.encode_message(reply))  # fits in a frame (raises otherwise)


async def test_bad_want_range_gets_an_empty_reply(world: World) -> None:
    syncer = Syncer(world.source)
    peer = FakePeer()
    await syncer.peer_active(peer)  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_want(world.admin.pubkey, 5, 1)))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_want(world.admin.pubkey, 1, 500)))  # type: ignore[arg-type]
    assert [message["entries"] for message in peer.sent if message["t"] == "ENTRIES"] == [[], []]


async def test_want_flood_is_rate_limited(world: World) -> None:
    syncer = Syncer(world.source, want_rate_limit=5)
    peer = FakePeer()
    await syncer.peer_active(peer)  # type: ignore[arg-type]
    want = wire(protocol.make_want(world.admin.pubkey, 1, 1))
    for _ in range(5):
        await syncer.handle(peer, want)  # type: ignore[arg-type]
    with pytest.raises(PeerClosed) as closed:
        await syncer.handle(peer, want)  # type: ignore[arg-type]
    assert closed.value.error_code == "rate_limited"


async def test_we_pace_our_own_wants(world: World) -> None:
    for _ in range(3):
        post_text(world.source, world.admin, world.key, "more")
    syncer, peer = await connected(world, want_send_limit=1)
    await syncer.handle(peer, world.heads(admin=5))  # type: ignore[arg-type]
    await syncer.handle(peer, wire(protocol.make_entries(world.raws(world.admin.pubkey, 1, 1))))  # type: ignore[arg-type]
    # The reply was partial, but we've used our one WANT for this window: we wait.
    assert peer.count("WANT") == 1
    assert syncer.peers[peer].retry_task is not None  # type: ignore[index]
    syncer.peer_closed(peer)  # type: ignore[arg-type]


# --- Chunks, home node side -------------------------------------------------------------


@pytest.fixture
def home_chunks(tmp_path: Path, world: World) -> ChunkStore:
    (tmp_path / "home-chunks").mkdir()
    return ChunkStore(tmp_path / "home-chunks", world.source)


def push_message(chunk: bytes, chunk_id: str | None = None) -> dict[str, Any]:
    return wire(protocol.make_chunk_push(chunk_id or crypto.blake2b_hex(chunk), base64.b64encode(chunk).decode()))


async def home_with_peer(world: World, chunks: ChunkStore, pubkey: str, **options: Any) -> tuple[Syncer, FakePeer]:
    syncer = Syncer(world.source, chunks=chunks, is_home=True, **options)
    peer = FakePeer("pusher", pubkey)
    await syncer.peer_active(peer)  # type: ignore[arg-type]
    return syncer, peer


async def test_home_stores_and_acks_a_members_chunk(world: World, home_chunks: ChunkStore) -> None:
    syncer, peer = await home_with_peer(world, home_chunks, world.bob.pubkey)
    chunk = crypto.encrypt_chunk(world.key, b"GIF89a pixels")
    await syncer.handle(peer, push_message(chunk))  # type: ignore[arg-type]
    assert peer.last("CHUNK_ACK")["id"] == crypto.blake2b_hex(chunk)
    assert home_chunks.load(crypto.blake2b_hex(chunk)) == chunk


async def test_home_refuses_pushes_from_non_members(world: World, home_chunks: ChunkStore) -> None:
    syncer, peer = await home_with_peer(world, home_chunks, Identity.generate().pubkey)
    await syncer.handle(peer, push_message(b"stranger danger"))  # type: ignore[arg-type]
    assert peer.count("CHUNK_ACK") == 0 and home_chunks.usage_bytes() == 0


async def test_home_refuses_a_chunk_that_doesnt_match_its_id(world: World, home_chunks: ChunkStore) -> None:
    syncer, peer = await home_with_peer(world, home_chunks, world.bob.pubkey)
    await syncer.handle(peer, push_message(b"real bytes", chunk_id="ab" * 32))  # type: ignore[arg-type]
    assert peer.count("CHUNK_ACK") == 0 and home_chunks.usage_bytes() == 0


async def test_push_over_the_quota_gets_quota_exceeded(world: World, home_chunks: ChunkStore) -> None:
    syncer, peer = await home_with_peer(world, home_chunks, world.bob.pubkey, storage_quota=100)
    await syncer.handle(peer, push_message(b"x" * 60))  # type: ignore[arg-type]
    with pytest.raises(PeerClosed) as closed:
        await syncer.handle(peer, push_message(b"y" * 60))  # type: ignore[arg-type]
    assert closed.value.error_code == "quota_exceeded"
    assert home_chunks.usage_bytes() == 60


async def test_a_member_node_ignores_pushes(world: World, home_chunks: ChunkStore) -> None:
    syncer = Syncer(world.source, chunks=home_chunks, is_home=False)
    peer = FakePeer("pusher", world.bob.pubkey)
    await syncer.peer_active(peer)  # type: ignore[arg-type]
    await syncer.handle(peer, push_message(b"not for you"))  # type: ignore[arg-type]
    assert peer.count("CHUNK_ACK") == 0


async def test_chunk_want_gets_chunks_and_missing(world: World, home_chunks: ChunkStore) -> None:
    chunk = crypto.encrypt_chunk(world.key, b"here")
    home_chunks.save(crypto.blake2b_hex(chunk), chunk)
    syncer, peer = await home_with_peer(world, home_chunks, world.bob.pubkey)
    await syncer.handle(peer, wire(protocol.make_chunk_want([crypto.blake2b_hex(chunk), "cd" * 32])))  # type: ignore[arg-type]
    assert base64.b64decode(peer.last("CHUNK")["data"]) == chunk
    assert peer.last("CHUNK_MISSING")["ids"] == ["cd" * 32]


async def test_chunk_want_flood_is_rate_limited(world: World, home_chunks: ChunkStore) -> None:
    syncer, peer = await home_with_peer(world, home_chunks, world.bob.pubkey, chunk_want_rate_limit=3)
    want = wire(protocol.make_chunk_want(["cd" * 32]))
    for _ in range(3):
        await syncer.handle(peer, want)  # type: ignore[arg-type]
    with pytest.raises(PeerClosed) as closed:
        await syncer.handle(peer, want)  # type: ignore[arg-type]
    assert closed.value.error_code == "rate_limited"


# --- Real nodes ------------------------------------------------------------------------


async def test_member_catches_up_with_admin(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    bob = await make_node("bob", start=False, listen=None, dial=[])
    seed_group(alice, bob)
    write_post(alice, "hello hearth")
    bob._dial = [DialTarget("127.0.0.1", alice.listen_port, alice.identity.pubkey)]  # type: ignore[attr-defined]
    await bob.start()
    await eventually(lambda: heads_of(bob) == heads_of(alice))
    assert feed.recent_posts(bob.store, bob.group_key, 10)[0]["text"] == "hello hearth"


async def test_live_post_reaches_a_connected_peer(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    bob = await make_node("bob", start=False, listen=None)
    seed_group(alice, bob)
    bob._dial = [DialTarget("127.0.0.1", alice.listen_port)]  # type: ignore[attr-defined]
    await bob.start()
    await eventually(lambda: heads_of(bob) == heads_of(alice))
    await alice.handle_control({"op": "post", "text": "fresh off the press", "images": []})
    await eventually(lambda: heads_of(bob) == heads_of(alice))
    # And bob can now post back, since he's a member on his own node too.
    await bob.handle_control({"op": "post", "text": "got it", "images": []})
    await eventually(lambda: heads_of(alice) == heads_of(bob))


async def test_over_two_mib_of_entries_syncs_in_several_frames(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    bob = await make_node("bob", start=False, listen=None)
    seed_group(alice, bob)
    for _ in range(60):
        write_post(alice, BIG_TEXT)
    total = sum(len(stored.raw.encode()) for stored in alice.store.get_range(alice.identity.pubkey, 1, 62))
    assert total > config.MAX_FRAME_BYTES
    bob._dial = [DialTarget("127.0.0.1", alice.listen_port)]  # type: ignore[attr-defined]
    await bob.start()
    await eventually(lambda: heads_of(bob) == heads_of(alice), timeout=15)
    assert bob.syncer.entries_messages_received >= 2


async def test_two_connections_between_the_same_nodes_dont_duplicate(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    bob = await make_node("bob", start=False, listen=None)
    seed_group(alice, bob)
    target = DialTarget("127.0.0.1", alice.listen_port)
    bob._dial = [target, target]  # type: ignore[attr-defined]
    await bob.start()
    await eventually(lambda: len(bob.active_peers()) == 2)
    for number in range(5):
        await alice.handle_control({"op": "post", "text": f"twice the pipes, same posts #{number}", "images": []})
    await eventually(lambda: heads_of(bob) == heads_of(alice))
    assert bob.store.entry_count() == alice.store.entry_count() == 7
    assert audit.verify(bob.store)["problems"] == []

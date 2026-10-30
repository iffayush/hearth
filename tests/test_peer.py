"""Tests for peer.py: key mapping, the session counter rule, and how a node
reacts to hostile or broken peers (without ever falling over itself).

Most tests drive a RawClient: a hand-operated peer built from the same pieces
as Peer, so it can send frames a real Peer never would.
"""

import asyncio
import logging
from typing import Any

import pytest
from nacl import bindings
from nacl.exceptions import CryptoError
from nacl.utils import random as random_bytes

from hearth import config, protocol
from hearth.identity import Identity
from hearth.network import allow_everyone
from hearth.node import Node
from hearth.peer import ACTIVE, CLOSED, Peer
from hearth.session import Session, derive_session_keys, handshake_transcript

from conftest import NodeFactory, eventually

READ_TIMEOUT = 5.0


@pytest.fixture
def make_node(make_node: NodeFactory) -> NodeFactory:
    """These tests are about the connection layer, not membership: let every
    authenticated peer in (strangers without a group included)."""

    async def factory(name: str, **options: Any) -> Node:
        options.setdefault("authorizer", allow_everyone)
        return await make_node(name, **options)

    return factory


class RawClient:
    """A peer driven by hand, one frame at a time."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, identity: Identity) -> None:
        self.reader = reader
        self.writer = writer
        self.identity = identity
        self.session: Session | None = None

    @staticmethod
    async def connect(node: Node, identity: Identity | None = None) -> "RawClient":
        reader, writer = await asyncio.open_connection("127.0.0.1", node.listen_port)
        return RawClient(reader, writer, identity or Identity.generate())

    async def handshake(self, forge_auth: bool = False) -> None:
        """Do the initiator's half of the handshake, exactly as Peer would (unless forging)."""
        eph_pk, eph_sk = bindings.crypto_kx_keypair()
        hello = protocol.make_hello(self.identity.pubkey, eph_pk.hex(), random_bytes(32).hex())
        await self.send_frame(protocol.encode_message(hello))
        their_hello = protocol.parse_message(await self.read_frame())
        transcript = handshake_transcript(hello, their_hello)
        if forge_auth:
            transcript = b"something else entirely"
        await self.send_frame(protocol.encode_message(protocol.make_auth(self.identity.sign(transcript))))
        if forge_auth:
            return
        protocol.parse_message(await self.read_frame())  # their AUTH
        rx, tx = derive_session_keys(True, eph_pk, eph_sk, bytes.fromhex(their_hello["eph"]))
        self.session = Session(tx_key=tx, rx_key=rx)

    def sealed(self, message: dict[str, Any]) -> bytes:
        assert self.session is not None
        return self.session.seal(protocol.encode_message(message))

    async def send_frame(self, payload: bytes) -> None:
        self.writer.write(len(payload).to_bytes(4, "big") + payload)
        await self.writer.drain()

    async def read_frame(self) -> bytes:
        async with asyncio.timeout(READ_TIMEOUT):
            return await protocol.read_frame(self.reader)

    async def wait_for(self, kind: str) -> dict[str, Any]:
        """Read and decrypt frames until one of type `kind` arrives (skipping the node's PINGs)."""
        assert self.session is not None
        while True:
            message = protocol.parse_message(self.session.open(await self.read_frame()))
            if message["t"] == kind:
                return message

    async def read_until_closed(self) -> list[dict[str, Any]]:
        """Collect everything the node says until it hangs up."""
        messages = []
        while True:
            try:
                payload = await self.read_frame()
            except (asyncio.IncompleteReadError, ConnectionError):
                return messages
            if self.session is not None:
                messages.append(protocol.parse_message(self.session.open(payload)))

    def close(self) -> None:
        self.writer.close()


async def connect_peer(node: Node, identity: Identity | None = None, **options: Any) -> tuple[Peer, asyncio.Task[None]]:
    """Dial `node` with a real Peer running in the background."""
    reader, writer = await asyncio.open_connection("127.0.0.1", node.listen_port)
    settings: dict[str, Any] = {"handshake_timeout": 1.0, "idle_timeout": 1.0, "ping_interval": 0.05, **options}
    peer = Peer(reader, writer, identity or Identity.generate(), initiator=True, authorizer=allow_everyone, **settings)
    return peer, asyncio.create_task(peer.run())


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def assert_node_still_works(node: Node) -> None:
    """After whatever just happened, a well-behaved peer can still connect and ping."""
    peer, task = await connect_peer(node)
    await eventually(lambda: peer.pongs_received >= 1)
    await stop(task)


def closed_cleanly(messages: list[dict[str, Any]], code: str) -> bool:
    return any(message["t"] == "ERROR" and message["code"] == code for message in messages)


# --- Pure pieces -------------------------------------------------------------


def test_crypto_kx_key_mapping() -> None:
    initiator_pk, initiator_sk = bindings.crypto_kx_keypair()
    responder_pk, responder_sk = bindings.crypto_kx_keypair()
    initiator_rx, initiator_tx = derive_session_keys(True, initiator_pk, initiator_sk, responder_pk)
    responder_rx, responder_tx = derive_session_keys(False, responder_pk, responder_sk, initiator_pk)
    assert initiator_tx == responder_rx
    assert initiator_rx == responder_tx
    assert initiator_tx != initiator_rx


def make_session_pair() -> tuple[Session, Session]:
    keys = [random_bytes(32), random_bytes(32)]
    return Session(tx_key=keys[0], rx_key=keys[1]), Session(tx_key=keys[1], rx_key=keys[0])


def test_session_frames_decrypt_in_order() -> None:
    sender, receiver = make_session_pair()
    frames = [sender.seal(f"frame {n}".encode()) for n in range(3)]
    assert [receiver.open(frame) for frame in frames] == [b"frame 0", b"frame 1", b"frame 2"]


def test_replay_attack_gets_left_on_read() -> None:
    sender, receiver = make_session_pair()
    frame = sender.seal(b"once")
    receiver.open(frame)
    with pytest.raises(CryptoError):
        receiver.open(frame)


def test_reordered_frames_fail() -> None:
    sender, receiver = make_session_pair()
    first, second = sender.seal(b"1"), sender.seal(b"2")
    with pytest.raises(CryptoError):
        receiver.open(second)


def test_failed_frame_still_uses_up_its_counter() -> None:
    sender, receiver = make_session_pair()
    first, second = sender.seal(b"1"), sender.seal(b"2")
    with pytest.raises(CryptoError):
        receiver.open(first[:-1] + bytes([first[-1] ^ 1]))
    # The counter moved on, so the next real frame still lines up.
    assert receiver.open(second) == b"2"


def test_transcript_is_fixed_length_and_refuses_odd_fields() -> None:
    hello = protocol.make_hello("aa" * 32, "bb" * 32, "cc" * 32)
    transcript = handshake_transcript(hello, hello)
    assert transcript == config.HANDSHAKE_SIGN_PREFIX + (("aa" * 32 + "bb" * 32 + "cc" * 32) * 2).encode()
    with pytest.raises(ValueError, match="64 hex"):
        handshake_transcript({**hello, "nonce": "cc"}, hello)


# --- Two real nodes ------------------------------------------------------------


async def test_two_nodes_handshake_and_ping(make_node: NodeFactory, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    alice = await make_node("alice")
    bob = await make_node("bob", listen=None, dial=[])
    peer, task = await connect_peer(alice, bob.identity, expected_pubkey=alice.identity.pubkey)
    await eventually(lambda: peer.pongs_received >= 1 and any(p.pongs_received for p in alice.active_peers()))
    assert peer.peer_pubkey == alice.identity.pubkey
    assert alice.active_peers()[0].peer_pubkey == bob.identity.pubkey
    # Logs name peers by their 8-character short IDs, never the full key.
    assert f"Handshake complete with {bob.identity.short_id}" in caplog.text
    assert f"PONG from {alice.identity.short_id}" in caplog.text
    await stop(task)


async def test_wrong_pinned_pubkey_aborts(make_node: NodeFactory, bob: Identity) -> None:
    alice = await make_node("alice")
    peer, task = await connect_peer(alice, expected_pubkey=bob.pubkey)
    await task
    assert peer.state == CLOSED and not peer.became_active.is_set()
    assert "doesn't match the pinned key" in (peer.close_reason or "")
    await assert_node_still_works(alice)


async def test_bob_cannot_pretend_to_be_alice(make_node: NodeFactory, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.handshake(forge_auth=True)
    assert await client.read_until_closed() == []
    await eventually(lambda: "bad handshake signature" in caplog.text)
    # Suspicious closes stand out in the log as warnings.
    assert any(record.levelname == "WARNING" and "bad handshake signature" in record.getMessage()
               for record in caplog.records)
    await assert_node_still_works(alice)


async def test_peer_claiming_to_be_us_is_refused(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    peer, task = await connect_peer(alice, alice.identity)
    await task
    assert not peer.became_active.is_set()
    await eventually(lambda: not alice.peers)
    await assert_node_still_works(alice)


async def test_handshake_timeout(make_node: NodeFactory, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    alice = await make_node("alice", handshake_timeout=0.05)
    client = await RawClient.connect(alice)
    # Say nothing at all. The node sends its HELLO, waits 0.05 s, and hangs up.
    hello = protocol.parse_message(await client.read_frame())
    assert hello["t"] == "HELLO"
    await client.read_until_closed()
    await eventually(lambda: "timed out" in caplog.text)
    assert not alice.peers


async def test_non_member_is_told_and_closed(make_node: NodeFactory) -> None:
    alice = await make_node("alice", authorizer=lambda pubkey: False)
    peer, task = await connect_peer(alice)
    await task
    assert "sent ERROR not_member" in (peer.close_reason or "")


# --- Hostile frames after the handshake ---------------------------------------


async def test_tampered_frame_closes_the_connection(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.handshake()
    frame = bytearray(client.sealed(protocol.make_ping()))
    frame[5] ^= 0x01
    await client.send_frame(bytes(frame))
    assert closed_cleanly(await client.read_until_closed(), "bad_frame")
    await assert_node_still_works(alice)


async def test_replayed_frame_closes_the_connection(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.handshake()
    ping = client.sealed(protocol.make_ping())
    await client.send_frame(ping)
    await client.wait_for("PONG")
    await client.send_frame(ping)  # the very same bytes again
    assert closed_cleanly(await client.read_until_closed(), "bad_frame")
    await assert_node_still_works(alice)


async def test_reordered_frames_close_the_connection(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.handshake()
    first, second = client.sealed(protocol.make_ping()), client.sealed(protocol.make_ping())
    await client.send_frame(second)
    await client.send_frame(first)
    assert closed_cleanly(await client.read_until_closed(), "bad_frame")


async def test_oversized_frame_closes_the_connection(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.handshake()
    # Only the header: the node must refuse it without waiting for 3 MiB.
    client.writer.write((3 * 2**20).to_bytes(4, "big"))
    await client.writer.drain()
    assert closed_cleanly(await client.read_until_closed(), "bad_frame")
    await assert_node_still_works(alice)


async def test_oversized_frame_during_handshake_closes(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    client.writer.write((3 * 2**20).to_bytes(4, "big"))
    await client.writer.drain()
    await client.read_until_closed()
    await eventually(lambda: not alice.peers)
    await assert_node_still_works(alice)


async def test_malformed_json_is_dropped_and_next_frame_still_works(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.handshake()
    assert client.session is not None
    await client.send_frame(client.session.seal(b"{this is not json"))
    await client.send_frame(client.sealed({"t": "GOSSIP", "about": "bob"}))
    await client.send_frame(client.sealed({"t": "PING", "extra": "field"}))
    await client.send_frame(client.sealed(protocol.make_ping()))
    assert (await client.wait_for("PONG"))["t"] == "PONG"
    assert alice.active_peers()[0].state == ACTIVE
    client.close()


async def test_garbage_instead_of_hello_closes(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    client = await RawClient.connect(alice)
    await client.send_frame(b"definitely not a HELLO")
    await client.read_frame()  # the node's own HELLO
    await client.read_until_closed()
    await eventually(lambda: not alice.peers)
    await assert_node_still_works(alice)


async def test_silent_peer_is_dropped_after_idle_timeout(make_node: NodeFactory) -> None:
    alice = await make_node("alice", idle_timeout=0.1, ping_interval=10)
    client = await RawClient.connect(alice)
    await client.handshake()
    await client.read_until_closed()  # we never send a thing; the node gives up
    await eventually(lambda: not alice.peers)

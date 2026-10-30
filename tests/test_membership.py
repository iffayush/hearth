"""The membership state machine end to end: LIMITED on the home node, JOINING on
the joiner, and the authorizer everywhere. Real nodes, plus a fake home node
(built from the same session pieces) to see exactly what a joiner sends."""

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from nacl import bindings
from nacl.public import PublicKey, SealedBox
from nacl.utils import random as random_bytes

from hearth import actions, crypto, feed, invite, protocol
from hearth.entry import create_entry, now_ms
from hearth.identity import Identity
from hearth.invite import InviteLink
from hearth.node import Node
from hearth.peer import CLOSED, Peer
from hearth.session import Session, derive_session_keys, handshake_transcript
from hearth.store import Store

from conftest import NodeFactory, eventually, seed_group, write_post
from test_peer import RawClient

Reply = Callable[[dict[str, Any]], dict[str, Any]] | None


async def home_with_invite(make_node: NodeFactory, **options: Any) -> tuple[Node, InviteLink]:
    home = await make_node("home", is_home=True, **options)
    seed_group(home)
    link = invite.create_invite(home.store, home.identity, "127.0.0.1", home.listen_port, 3600, now_ms())
    return home, link


async def finished(peer: Peer) -> None:
    await eventually(lambda: peer.became_active.is_set() or peer.state == CLOSED)


# --- Real home node, real joiner -------------------------------------------------------------


async def test_join_receives_the_key_and_the_history(make_node: NodeFactory) -> None:
    home, link = await home_with_invite(make_node)
    write_post(home, "before you arrived")
    carol = await make_node("carol", listen=None)
    saved: list[bytes] = []
    peer = await carol.join(link, saved.append)
    await finished(peer)
    assert peer.became_active.is_set(), peer.close_reason
    assert saved == [home.group_key] and carol.group_key == home.group_key
    assert carol.store.group_id() == home.store.group_id()
    # The admin's log arrives, including carol's own member_add, and she can read the old post.
    await eventually(lambda: carol.identity.pubkey in carol.store.members() and carol.syncer.caught_up(peer))
    assert feed.recent_posts(carol.store, carol.group_key, 5)[0]["text"] == "before you arrived"


async def test_reused_link_is_refused_and_nothing_is_saved(make_node: NodeFactory) -> None:
    home, link = await home_with_invite(make_node)
    carol = await make_node("carol", listen=None)
    await finished(await carol.join(link, lambda key: None))
    mallory = await make_node("mallory", listen=None)
    saved: list[bytes] = []
    peer = await mallory.join(link, saved.append)
    await finished(peer)
    assert not peer.became_active.is_set()
    assert peer.error_received == "bad_invite" and "already been used" in (peer.close_reason or "")
    assert saved == [] and mallory.store.group_id() is None and mallory.group_key is None


async def test_expired_link_is_refused(make_node: NodeFactory) -> None:
    home = await make_node("home", is_home=True)
    seed_group(home)
    two_hours_ago = now_ms() - 7200 * 1000
    link = invite.create_invite(home.store, home.identity, "127.0.0.1", home.listen_port, 3600, two_hours_ago)
    carol = await make_node("carol", listen=None)
    peer = await carol.join(link, lambda key: None)
    await finished(peer)
    assert peer.error_received == "bad_invite" and "expired" in (peer.close_reason or "")


async def test_joining_again_after_a_lost_invite_ok_explains_itself(make_node: NodeFactory) -> None:
    """The owner chose: no retries. If INVITE_OK is lost, the identity is a member without the key;
    another attempt with that identity is told to start over with a fresh profile."""
    home, link = await home_with_invite(make_node)
    carol = await make_node("carol", listen=None)
    # Redeem behind carol's back, as if her INVITE_OK had been lost on the way.
    invite.redeem(home.store, home.identity, home.group_key, link.token, carol.identity.pubkey,
                  carol.identity.encryption_pubkey, now_ms())
    second = invite.create_invite(home.store, home.identity, "127.0.0.1", home.listen_port, 3600, now_ms())
    saved: list[bytes] = []
    peer = await carol.join(second, saved.append)
    await finished(peer)
    assert not peer.became_active.is_set() and saved == []
    assert "already counts this identity as a member" in (peer.close_reason or "")


async def test_invite_redemption_is_rate_limited_per_ip(make_node: NodeFactory) -> None:
    home, link = await home_with_invite(make_node, invite_rate_limit=2)
    bad = InviteLink(link.host, link.port, link.home_pubkey, link.group, "B" * 43)
    codes = []
    for attempt in range(3):
        stranger = await make_node(f"stranger{attempt}", listen=None)
        peer = await stranger.join(bad, lambda key: None)
        await finished(peer)
        codes.append(peer.error_received)
    assert codes == ["bad_invite", "bad_invite", "rate_limited"]


# --- LIMITED: what a non-member gets on the home node ----------------------------------------


async def test_non_member_receives_nothing_unprompted(make_node: NodeFactory) -> None:
    home, _ = await home_with_invite(make_node, idle_timeout=0.3)
    write_post(home, "members only")
    stranger = await RawClient.connect(home)
    await stranger.handshake()
    # Say nothing: the home node sends nothing either (no HEADS, no PING) and,
    # after its idle timeout, simply hangs up.
    assert await stranger.read_until_closed() == []


async def test_non_member_is_closed_on_anything_but_invite_redeem(make_node: NodeFactory) -> None:
    home, _ = await home_with_invite(make_node)
    stranger = await RawClient.connect(home)
    await stranger.handshake()
    await stranger.send_frame(stranger.sealed(protocol.make_heads({})))
    messages = await stranger.read_until_closed()
    assert [(message["t"], message["code"]) for message in messages] == [("ERROR", "not_member")]


async def test_non_member_on_a_member_node_is_turned_away(make_node: NodeFactory) -> None:
    member = await make_node("member")  # not the home node: no invites here
    seed_group(member)
    stranger = await RawClient.connect(member)
    await stranger.handshake()
    messages = await stranger.read_until_closed()
    assert [(message["t"], message["code"]) for message in messages] == [("ERROR", "not_member")]


async def test_members_get_heads_straight_away(make_node: NodeFactory) -> None:
    home = await make_node("home", is_home=True)
    bob = Identity.generate()
    seed_group(home)
    actions.add_member(home.store, home.identity, bob.pubkey)
    client = await RawClient.connect(home, bob)
    await client.handshake()
    assert (await client.wait_for("HEADS"))["t"] == "HEADS"
    client.close()


def test_posts_by_non_members_are_rejected(tmp_path: Path) -> None:
    """(Also in test_store.py; repeated because it's on Phase 6's list.)"""
    store = Store.open(tmp_path / "x.db")
    admin, outsider = Identity.generate(), Identity.generate()
    group = actions.create_group(store, admin)["group"]
    box = crypto.seal_box(crypto.new_group_key(), {"text": "let me in", "images": []})
    result = store.append_entry(create_entry(outsider, group, None, "post", {"box": box}, 1).raw())
    assert result.reason is not None and result.reason.code == "not_member"
    store.close()


# --- JOINING: a fake home node that records what the joiner sends ---------------------------


class FakeHome:
    """Accepts a connection, does the responder's half of the handshake, records every
    message the joiner sends, and answers INVITE_REDEEM with `reply` (None: stay silent)."""

    def __init__(self, identity: Identity, reply: Reply) -> None:
        self.identity = identity
        self.reply = reply
        self.received: list[dict[str, Any]] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        eph_pk, eph_sk = bindings.crypto_kx_keypair()
        their_hello = protocol.parse_message(await protocol.read_frame(reader))
        own_hello = protocol.make_hello(self.identity.pubkey, eph_pk.hex(), random_bytes(32).hex())
        auth = protocol.make_auth(self.identity.sign(handshake_transcript(their_hello, own_hello)))
        for message in (own_hello, auth):
            writer.write(protocol.encode_frame(protocol.encode_message(message)))
        try:
            protocol.parse_message(await protocol.read_frame(reader))  # their AUTH
            rx, tx = derive_session_keys(False, eph_pk, eph_sk, bytes.fromhex(their_hello["eph"]))
            session = Session(tx_key=tx, rx_key=rx)
            while True:
                message = protocol.parse_message(session.open(await protocol.read_frame(reader)))
                self.received.append(message)
                if message["t"] == "INVITE_REDEEM" and self.reply is not None:
                    writer.write(protocol.encode_frame(session.seal(protocol.encode_message(self.reply(message)))))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()


async def join_fake_home(make_node: NodeFactory, reply: Reply, **options: Any) -> tuple[Node, Peer, FakeHome, list[bytes]]:
    home_identity = Identity.generate()
    fake = FakeHome(home_identity, reply)
    await fake.start()
    link = InviteLink("127.0.0.1", fake.port, home_identity.pubkey, "ab" * 32, "C" * 43)
    carol = await make_node("carol", listen=None, **options)
    saved: list[bytes] = []
    peer = await carol.join(link, saved.append)
    await finished(peer)
    fake.stop()
    return carol, peer, fake, saved


def seal_to(enc_pubkey: str, group_key: bytes) -> str:
    return base64.b64encode(bytes(SealedBox(PublicKey(bytes.fromhex(enc_pubkey))).encrypt(group_key))).decode()


async def test_joiner_sends_nothing_but_invite_redeem_before_invite_ok(make_node: NodeFactory) -> None:
    # The fake home never answers; after its (short) join timeout the joiner gives up.
    carol, peer, fake, saved = await join_fake_home(make_node, reply=None, join_timeout=0.3)
    assert [message["t"] for message in fake.received] == ["INVITE_REDEEM"]
    assert fake.received[0]["enc_pubkey"] == carol.identity.encryption_pubkey
    assert "no word from" in (peer.close_reason or "") and saved == []


@pytest.mark.parametrize("problem", ["wrong group", "wrong admin", "won't unseal"])
async def test_bad_invite_ok_is_rejected_and_nothing_is_saved(make_node: NodeFactory, problem: str) -> None:
    group_key = crypto.new_group_key()
    home_identity = Identity.generate()

    def reply(message: dict[str, Any]) -> dict[str, Any]:
        recipient = message["enc_pubkey"] if problem != "won't unseal" else Identity.generate().encryption_pubkey
        group = "ee" * 32 if problem == "wrong group" else "ab" * 32
        # The link pins the fake home's real key; "wrong admin" names someone else in INVITE_OK.
        admin = "ee" * 32 if problem == "wrong admin" else home_identity.pubkey
        return protocol.make_invite_ok(group, admin, seal_to(recipient, group_key))

    fake = FakeHome(home_identity, reply)
    await fake.start()
    link = InviteLink("127.0.0.1", fake.port, home_identity.pubkey, "ab" * 32, "C" * 43)
    carol = await make_node("carol", listen=None)
    saved: list[bytes] = []
    peer = await carol.join(link, saved.append)
    await finished(peer)
    fake.stop()
    assert not peer.became_active.is_set()
    assert "rejected INVITE_OK" in (peer.close_reason or "")
    assert saved == [] and carol.store.group_id() is None and carol.group_key is None


async def test_joiner_only_trusts_the_key_in_the_link(make_node: NodeFactory) -> None:
    impostor = Identity.generate()
    fake = FakeHome(impostor, reply=None)
    await fake.start()
    link = InviteLink("127.0.0.1", fake.port, Identity.generate().pubkey, "ab" * 32, "C" * 43)
    carol = await make_node("carol", listen=None)
    peer = await carol.join(link, lambda key: None)
    await finished(peer)
    fake.stop()
    assert "doesn't match the pinned key" in (peer.close_reason or "")
    assert fake.received == []  # the token never left carol's node

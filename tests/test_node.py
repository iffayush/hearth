"""Tests for node.py: addresses, backoff, redialling, connection limits and shutdown."""

import asyncio
import logging
import socket
from typing import Any

import pytest

from hearth.network import (
    AttemptLimiter, DialTarget, allow_everyone, jittered, next_delay, parse_address, parse_listen_address,
)
from hearth.node import Node

from conftest import FAST_TIMEOUTS, NodeFactory, eventually
from test_peer import RawClient


@pytest.fixture
def make_node(make_node: NodeFactory) -> NodeFactory:
    """These tests are about the connection layer, not membership: let every
    authenticated peer in (strangers without a group included)."""

    async def factory(name: str, **options: Any) -> Node:
        options.setdefault("authorizer", allow_everyone)
        return await make_node(name, **options)

    return factory


def free_port() -> int:
    """A port nothing is listening on right now (the OS picks it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- Addresses -------------------------------------------------------------------


def test_parse_address() -> None:
    assert parse_address("127.0.0.1:7001") == ("127.0.0.1", 7001)
    assert parse_address("home.example.net:7777") == ("home.example.net", 7777)


@pytest.mark.parametrize("text", ["127.0.0.1", ":7777", "host:", "host:http", "host:0", "host:70000"])
def test_bad_addresses_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_address(text)


@pytest.mark.parametrize("text", ["localhost:7777", "[::1]:7777", "home.example.net:7777"])
def test_listen_address_must_be_ipv4(text: str) -> None:
    with pytest.raises(ValueError, match="IPv4"):
        parse_listen_address(text)


# --- Backoff -------------------------------------------------------------------------


def test_backoff_doubles_up_to_the_cap() -> None:
    delays = [1.0]
    for _ in range(8):
        delays.append(next_delay(delays[-1], 60.0))
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]


def test_attempt_limiter_counts_per_key_within_its_window() -> None:
    now = [1000.0]
    limiter = AttemptLimiter(limit=2, window=600, clock=lambda: now[0])
    assert limiter.allow("10.0.0.1") and limiter.allow("10.0.0.1")
    assert not limiter.allow("10.0.0.1")
    # Someone else's attempts don't count against you.
    assert limiter.allow("10.0.0.2")
    now[0] += 601
    assert limiter.allow("10.0.0.1")


def test_jitter_stays_within_twenty_percent() -> None:
    samples = [jittered(10.0, 0.2) for _ in range(1000)]
    assert all(8.0 <= sample <= 12.0 for sample in samples)
    assert len(set(samples)) > 1


# --- Dialling --------------------------------------------------------------------------


async def test_dialler_keeps_trying_until_the_other_node_appears(make_node: NodeFactory) -> None:
    port = free_port()
    bob = await make_node("bob", listen=None, dial=[DialTarget("127.0.0.1", port)])
    # Nobody's home yet; bob is backing off and retrying.
    alice = await make_node("alice", listen=("127.0.0.1", port))
    await eventually(lambda: any(peer.pongs_received for peer in bob.active_peers()))
    assert bob.active_peers()[0].peer_pubkey == alice.identity.pubkey


async def test_dialler_reconnects_after_the_other_node_restarts(make_node: NodeFactory) -> None:
    port = free_port()
    alice = await make_node("alice", listen=("127.0.0.1", port))
    bob = await make_node("bob", listen=None, dial=[DialTarget("127.0.0.1", port, alice.identity.pubkey)])
    await eventually(lambda: bool(bob.active_peers()))
    await alice.stop()
    await eventually(lambda: not bob.active_peers())

    # Same identity, same port: the home node came back after a reboot.
    alice_again = Node(alice.identity, alice.store, alice.chunks, authorizer=allow_everyone, listen=("127.0.0.1", port), **FAST_TIMEOUTS)
    await alice_again.start()
    try:
        await eventually(lambda: any(peer.pongs_received for peer in bob.active_peers()))
    finally:
        await alice_again.stop()


async def test_pinned_dial_to_the_wrong_node_never_activates(
    make_node: NodeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    alice = await make_node("alice")
    mallory = await make_node("mallory")
    bob = await make_node(
        "bob", listen=None, dial=[DialTarget("127.0.0.1", mallory.listen_port, alice.identity.pubkey)]
    )
    # Mallory answers, bob checks her key against alice's and hangs up, then
    # backs off and tries again. Wait for a few rounds of that.
    await eventually(lambda: caplog.text.count("doesn't match the pinned key") >= 3)
    assert not bob.active_peers()


# --- Limits and shutdown ---------------------------------------------------------------


async def connection_refused(client: RawClient) -> bool:
    """True if the node closed this connection without even saying HELLO."""
    try:
        await client.read_frame()
    except (asyncio.IncompleteReadError, ConnectionError):
        return True
    return False


async def test_total_connection_limit(make_node: NodeFactory) -> None:
    alice = await make_node("alice", max_connections=2, handshake_timeout=5)
    first, second = await RawClient.connect(alice), await RawClient.connect(alice)
    assert not await connection_refused(first) and not await connection_refused(second)
    third = await RawClient.connect(alice)
    assert await connection_refused(third)
    first.close()
    # A slot frees up once the first connection is gone.
    await eventually(lambda: len(alice.peers) == 1)
    fourth = await RawClient.connect(alice)
    assert not await connection_refused(fourth)
    for client in (second, fourth):
        client.close()


async def test_per_ip_connection_limit(make_node: NodeFactory) -> None:
    alice = await make_node("alice", max_connections_per_ip=1, handshake_timeout=5)
    first = await RawClient.connect(alice)
    assert not await connection_refused(first)
    second = await RawClient.connect(alice)
    assert await connection_refused(second)
    first.close()


async def test_stop_closes_every_connection(make_node: NodeFactory) -> None:
    alice = await make_node("alice")
    bob = await make_node("bob", listen=None, dial=[DialTarget("127.0.0.1", alice.listen_port)])
    await eventually(lambda: bool(alice.active_peers()) and bool(bob.active_peers()))
    await bob.stop()
    assert not bob.peers
    await eventually(lambda: not alice.peers)

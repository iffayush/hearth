"""A home node behind a front door like Tailscale Funnel, end to end.

`FrontDoor` stands in for Funnel: friends reach it with TLS, and it hands each
connection to the home node in plain TCP, starting with a PROXY v2 header that
says who the friend really is. Through it: members sync, a friend joins with an
`h=tls://` invite link, and the per-IP limits count friends rather than the door.
A caller who skips the door, or a public --proxy-protocol listener, is refused.

Joke: the bouncer checks the coat, the host checks the secret handshake, and
nobody checks the dress code.
"""

import asyncio
import itertools
import logging
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from hearth import feed, invite, settings
from hearth.entry import now_ms
from hearth.network import DialTarget
from hearth.peer import CLOSED

from conftest import NodeFactory, eventually, seed_group, write_post
from test_cli import alice_with_group, keyed, run_hearth
from test_cli_run import init_profile, join, start_run, wait_for_log_line
from test_node import free_port
from test_transport import TEST_CERT, proxy_header


class FrontDoor:
    """TLS outside; inside, a plain connection to the home node that starts with a
    PROXY v2 header naming the next address from `callers`, as if the friend came from it."""

    def __init__(self, home_port: int, callers: Iterator[str]) -> None:
        self.home_port = home_port
        self.callers = callers
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(TEST_CERT)
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=context)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        home_reader, home_writer = await asyncio.open_connection("127.0.0.1", self.home_port)
        self._writers += [writer, home_writer]
        home_writer.write(proxy_header(next(self.callers), 40000 + len(self._writers)))
        await asyncio.gather(pipe(reader, home_writer), pipe(home_reader, writer))

    def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in self._writers:
            writer.transport.abort()


async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    """Copy bytes one way until either side hangs up."""
    try:
        while data := await source.read(65536):
            sink.write(data)
            await sink.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        sink.close()


DoorFactory = Callable[..., Awaitable[FrontDoor]]


@pytest.fixture
async def front_door(make_node: NodeFactory) -> AsyncIterator[DoorFactory]:
    """`await front_door(home_port, "203.0.113.1", ...)`. Asks for make_node so the
    doors close first, before the nodes behind them stop."""
    doors: list[FrontDoor] = []

    async def factory(home_port: int, *callers: str) -> FrontDoor:
        door = FrontDoor(home_port, itertools.cycle(callers))
        await door.start()
        doors.append(door)
        return door

    yield factory
    for door in doors:
        door.stop()


async def test_friends_behind_the_door_count_by_their_own_address(
    make_node: NodeFactory, front_door: DoorFactory
) -> None:
    # One connection per IP: if both friends looked like the door (127.0.0.1), only one would get in.
    home = await make_node("home", is_home=True, proxy_protocol=True, max_connections_per_ip=1)
    door = await front_door(home.listen_port, "203.0.113.1", "203.0.113.2")
    through_door = DialTarget("127.0.0.1", door.port, home.identity.pubkey, tls=True)
    alice = await make_node("alice", listen=None, dial=[through_door], start=False)
    bob = await make_node("bob", listen=None, dial=[through_door], start=False)
    seed_group(home, alice, bob)
    for member in (alice, bob):
        await member.start()
    await eventually(lambda: len(home.active_peers()) == 2)
    assert {peer.remote_ip for peer in home.active_peers()} == {"203.0.113.1", "203.0.113.2"}

    # alice's own member_add has to reach her (from home, through the door) before she can post.
    await eventually(lambda: alice.identity.pubkey in alice.store.members())
    write_post(alice, "through the door")
    assert bob.group_key is not None
    await eventually(lambda: any(post["text"] == "through the door"
                                 for post in feed.recent_posts(bob.store, bob.group_key, 5)))


async def test_joining_with_a_tls_invite_link(make_node: NodeFactory, front_door: DoorFactory) -> None:
    home = await make_node("home", is_home=True, proxy_protocol=True)
    seed_group(home)
    door = await front_door(home.listen_port, "198.51.100.23")
    made = invite.create_invite(home.store, home.identity, "127.0.0.1", door.port, 3600, now_ms(), tls=True)
    assert made.text().startswith(f"hearth://join?h=tls://127.0.0.1:{door.port}&k=")
    link = invite.parse_link(made.text())
    assert link == made and link.tls

    carol = await make_node("carol", listen=None)
    peer = await carol.join(link, lambda key: None)
    await eventually(lambda: peer.became_active.is_set() or peer.state == CLOSED)
    assert peer.became_active.is_set(), peer.close_reason
    assert carol.identity.pubkey in home.store.members()
    # The invite limiter (5 tries per IP) saw carol's address, not the door's.
    assert [joiner.remote_ip for joiner in home.peers] == ["198.51.100.23"]


async def test_a_caller_who_skips_the_door_is_dropped(
    make_node: NodeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="hearth.transport")
    home = await make_node("home", is_home=True, proxy_protocol=True)
    stray = await make_node("stray", listen=None,
                            dial=[DialTarget("127.0.0.1", home.listen_port, home.identity.pubkey)])
    await eventually(lambda: "Dropped a connection from 127.0.0.1" in caplog.text)
    assert not home.peers and not stray.active_peers()


async def test_a_crawler_at_the_door_is_named_by_its_own_address(
    make_node: NodeFactory, front_door: DoorFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="hearth.peer")
    home = await make_node("home", is_home=True, proxy_protocol=True)
    door = await front_door(home.listen_port, "203.0.113.9")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    _, writer = await asyncio.open_connection("127.0.0.1", door.port, ssl=context)
    writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    await eventually(lambda: "a web request, not Hearth" in caplog.text)
    writer.transport.abort()
    assert "203.0.113.9" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.parametrize("flags", [["--role", "home", "--listen", "0.0.0.0:7777"], []])
def test_proxy_protocol_needs_a_loopback_listener(tmp_path: Path, passphrase_file: Path, flags: list[str]) -> None:
    """On a public port (or none: a member), anyone could claim any address in a PROXY header."""
    data_dir = alice_with_group(tmp_path, passphrase_file)
    result = run_hearth(*keyed(["run", "--headless", "--proxy-protocol", *flags], data_dir, passphrase_file))
    assert result.returncode == 1 and "--listen 127.0.0.1:7777" in result.stderr


async def test_the_cli_way_invite_join_and_run(tmp_path: Path, passphrase_file: Path, front_door: DoorFactory) -> None:
    """docs/deploy.md's path C with the stand-in door: `invite --host tls://...`, `join`, then `run`."""
    alice_dir, _ = init_profile(tmp_path, "alice", passphrase_file)
    assert run_hearth(*keyed(["create-group"], alice_dir, passphrase_file)).returncode == 0
    port = free_port()
    alice = await start_run(alice_dir, passphrase_file, "--role", "home", "--listen", f"127.0.0.1:{port}",
                            "--proxy-protocol")
    bob = None
    try:
        await wait_for_log_line(alice, "Listening on")
        door = await front_door(port, "203.0.113.7")
        invited = await asyncio.to_thread(
            run_hearth, *keyed(["invite", "--host", f"tls://127.0.0.1:{door.port}"], alice_dir, passphrase_file))
        link = next(line for line in invited.stdout.splitlines() if line.startswith("hearth://"))
        assert link.startswith(f"hearth://join?h=tls://127.0.0.1:{door.port}&"), invited
        joined = await join(tmp_path, "bob", link, passphrase_file)
        assert joined.returncode == 0, joined.stdout + joined.stderr
        assert "(203.0.113.7:" in await wait_for_log_line(alice, "Handshake complete with")
        # bob's `run` dials the saved tls:// address, through the door again.
        assert settings.load_settings(tmp_path / "bob").home_host == f"tls://127.0.0.1:{door.port}"
        bob = await start_run(tmp_path / "bob", passphrase_file)
        await wait_for_log_line(bob, "PONG from")
    finally:
        for process in (alice, bob):
            if process is not None:
                process.kill()
                await process.wait()

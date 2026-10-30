"""When the other side doesn't speak Hearth at all: crawlers at the door, wrong ports in links.

A crawler's "GET " and a web server's "HTTP" arrive where a frame length belongs.
Callers like that get one quiet line and a closed connection, and anyone we
dialled gets named, so a person can fix the address. Real garbage, and a member
misbehaving after the handshake, still count as suspicious.

Joke: we asked for the secret handshake and got a résumé.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from aiohttp import web

from hearth import protocol
from hearth.cli import CliError
from hearth.cli_setup import join_and_sync
from hearth.identity import Identity
from hearth.invite import InviteLink
from hearth.node import Node
from hearth.peer import CLOSED

from conftest import NodeFactory, eventually
from test_node import free_port
from test_peer import RawClient

ServerFactory = Callable[[bytes], Awaitable[int]]
PEER_LOGGER = "hearth.peer"
TLS_HELLO = b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc"
TLS_ALERT = b"\x15\x03\x03\x00\x02\x02\x46"


def reader_with(data: bytes) -> asyncio.StreamReader:
    # No EOF: a read past `data` would wait forever, and the test's timeout would fire.
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    return reader


def link_to(port: int) -> InviteLink:
    return InviteLink("127.0.0.1", port, Identity.generate().pubkey, "ab" * 32, "C" * 43)


def peer_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == PEER_LOGGER]


@pytest.fixture
async def answering_server() -> AsyncIterator[ServerFactory]:
    """`port = await answering_server(b"...")`: says that to whoever sends it anything, then hangs up."""
    servers: list[asyncio.Server] = []

    async def handle(answer: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1)
        writer.write(answer)
        await writer.drain()
        writer.close()

    async def factory(answer: bytes) -> int:
        server = await asyncio.start_server(lambda r, w: handle(answer, r, w), "127.0.0.1", 0)
        servers.append(server)
        return server.sockets[0].getsockname()[1]

    yield factory
    for server in servers:
        server.close()


@pytest.fixture
async def web_server() -> AsyncIterator[int]:
    """The port of a real aiohttp server, as an invite pointing at the web UI would reach."""
    runner = web.AppRunner(web.Application())
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    yield port
    await runner.cleanup()


# --- Recognising the first 4 bytes -----------------------------------------------------------


@pytest.mark.parametrize(("header", "speaks"), [
    (b"GET ", "HTTP"), (b"POST", "HTTP"), (b"HEAD", "HTTP"), (b"PRI ", "HTTP"), (b"HTTP", "HTTP"),
    (TLS_HELLO[:4], "TLS"), (TLS_ALERT[:4], "TLS"),
])
async def test_another_protocols_start_is_named(header: bytes, speaks: str) -> None:
    with pytest.raises(protocol.ForeignProtocol) as raised:
        async with asyncio.timeout(2):
            await protocol.read_frame(reader_with(header))
    assert raised.value.speaks == speaks
    # Still a refused frame to anything that only knows about those.
    assert isinstance(raised.value, protocol.FrameTooLarge)


@pytest.mark.parametrize("header", [
    (3 * 2**20).to_bytes(4, "big"),  # just too long
    b"\x7f\xff\xff\xff",
    b"\x16\x02\x00\x00",  # a TLS record type, but not version 3
    b"get ",  # HTTP methods are upper case
])
async def test_other_oversized_frames_stay_plain_refusals(header: bytes) -> None:
    with pytest.raises(protocol.FrameTooLarge, match="moving truck") as raised:
        async with asyncio.timeout(2):
            await protocol.read_frame(reader_with(header))
    assert not isinstance(raised.value, protocol.ForeignProtocol)


async def test_a_frame_within_the_limit_is_never_mistaken_for_another_protocol() -> None:
    header = protocol.encode_frame(b"x" * 8)[:4]
    assert protocol.foreign_protocol(header) is None
    assert await protocol.read_frame(reader_with(header + b"x" * 8)) == b"x" * 8


# --- Strangers calling the home node ---------------------------------------------------------


@pytest.mark.parametrize(("first_bytes", "line"), [
    (b"GET /.env HTTP/1.1\r\nHost: x\r\n\r\n", "a web request, not Hearth"),
    (TLS_HELLO, "a TLS connection, but this node doesn't use TLS"),
])
async def test_a_stranger_speaking_something_else_gets_one_quiet_line(
    make_node: NodeFactory, caplog: pytest.LogCaptureFixture, first_bytes: bytes, line: str
) -> None:
    caplog.set_level(logging.INFO, logger=PEER_LOGGER)
    home = await make_node("home", is_home=True)
    client = await RawClient.connect(home)
    client.writer.write(first_bytes)
    await eventually(lambda: line in caplog.text)
    client.writer.close()
    assert [record.levelno for record in peer_records(caplog)] == [logging.INFO]
    assert "Frame too big" not in caplog.text and not home.active_peers()


async def test_real_garbage_is_still_a_warning(make_node: NodeFactory, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=PEER_LOGGER)
    home = await make_node("home", is_home=True)
    client = await RawClient.connect(home)
    client.writer.write(b"\x7f\xff\xff\xff")
    await eventually(lambda: "Frame too big" in caplog.text)
    client.writer.close()
    assert [record.levelno for record in peer_records(caplog)] == [logging.WARNING]


async def test_a_member_sending_http_after_the_handshake_is_still_a_warning(
    make_node: NodeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """Being quiet is for strangers at the door. Inside a session this is a broken or hostile peer."""
    caplog.set_level(logging.INFO, logger=PEER_LOGGER)
    member = Identity.generate()
    home = await make_node("home", authorizer=lambda pubkey: pubkey == member.pubkey)
    client = await RawClient.connect(home, member)
    await client.handshake()
    client.writer.write(b"GET ")
    await eventually(lambda: "this is HTTP, not Hearth" in caplog.text)
    client.writer.close()
    closes = [record for record in peer_records(caplog) if "Closed connection" in record.getMessage()]
    assert [record.levelno for record in closes] == [logging.WARNING]


# --- Us dialling the wrong place -------------------------------------------------------------


async def join_fails(node: Node, link: InviteLink) -> str:
    with pytest.raises(CliError) as raised:
        await join_and_sync(node, link, "Carol", lambda key: None)
    return str(raised.value)


async def test_an_invite_pointing_at_a_web_page_says_so(make_node: NodeFactory, web_server: int) -> None:
    carol = await make_node("carol", listen=None, start=False)
    message = await join_fails(carol, link_to(web_server))
    assert "answered like a web server, not a Hearth node" in message and "check the port" in message
    assert "corrected link" in message
    # The link's address is wrong, so the old advice to try it again can't help.
    assert "retry" not in message and "MiB" not in message


async def test_a_link_missing_tls_says_so(make_node: NodeFactory, answering_server: ServerFactory) -> None:
    port = await answering_server(TLS_ALERT)
    carol = await make_node("carol", listen=None, start=False)
    message = await join_fails(carol, link_to(port))
    assert "answered with TLS" in message and "tls://" in message and "corrected link" in message


async def test_a_join_that_fails_some_other_way_can_still_be_retried(
    make_node: NodeFactory, answering_server: ServerFactory
) -> None:
    port = await answering_server(b"")
    carol = await make_node("carol", listen=None, start=False)
    message = await join_fails(carol, link_to(port))
    assert "You can retry the same link" in message and "corrected" not in message


async def test_a_wrong_address_is_a_warning_for_the_one_who_dialled_it(
    make_node: NodeFactory, answering_server: ServerFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=PEER_LOGGER)
    port = await answering_server(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    carol = await make_node("carol", listen=None)
    peer = await carol.join(link_to(port), lambda key: None)
    await eventually(lambda: peer.state == CLOSED)
    assert peer.not_hearth
    # The state changes first; the line is logged once the socket has finished closing.
    await eventually(lambda: peer_records(caplog))
    assert [record.levelno for record in peer_records(caplog)] == [logging.WARNING]

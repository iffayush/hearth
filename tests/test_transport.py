"""Tests for transport.py and tls:// addresses: PROXY v2 headers byte by byte,
TLS dialling (with SNI, without certificate checks), and the address forms.

The front door as a whole (TLS outside, PROXY header inside, real nodes) is in
test_front_door.py.

Joke: we parse the header that says who you really are. Like a name badge,
but in network byte order.
"""

import asyncio
import ipaddress
import logging
import ssl
import struct
from pathlib import Path

import pytest

from hearth.network import DialTarget, parse_dial_address
from hearth.transport import PROXY_V2_SIGNATURE, ProxyHeaderError, open_stream, read_proxy_header, remote_address

TEST_CERT = Path(__file__).parent / "data" / "front-door-test.pem"

# HAProxy's spec, written out by hand: PROXY over TCP4, 192.0.2.1:56324 -> 127.0.0.1:443.
IPV4_EXAMPLE = (b"\r\n\r\n\x00\r\nQUIT\n" + b"\x21\x11\x00\x0c" + bytes([192, 0, 2, 1]) + bytes([127, 0, 0, 1])
                + (56324).to_bytes(2, "big") + (443).to_bytes(2, "big"))


def proxy_header(ip: str, port: int, command: int = 1, family: int | None = None, tlvs: bytes = b"") -> bytes:
    """A PROXY v2 header claiming the caller is ip:port (what tailscaled or HAProxy would send)."""
    source = ipaddress.ip_address(ip)
    destination = ipaddress.ip_address("127.0.0.1" if source.version == 4 else "::1")
    body = source.packed + destination.packed + struct.pack("!HH", port, 443) + tlvs
    family = family if family is not None else (0x11 if source.version == 4 else 0x21)
    return PROXY_V2_SIGNATURE + struct.pack("!BBH", 0x20 | command, family, len(body)) + body


def stream_of(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


# --- PROXY v2 ----------------------------------------------------------------------------


async def test_ipv4_header_names_the_caller_and_leaves_the_rest() -> None:
    reader = stream_of(IPV4_EXAMPLE + b"HELLO")
    assert await read_proxy_header(reader) == ("192.0.2.1", 56324)
    # Hearth's own first frame is untouched behind it.
    assert await reader.read() == b"HELLO"


async def test_ipv6_header_with_tlvs_after_the_addresses() -> None:
    # A made-up TLV (type 0xEE, 3 bytes): whatever the front door adds, we read past it.
    reader = stream_of(proxy_header("2001:db8::7", 40001, tlvs=b"\xee\x00\x03abc") + b"HELLO")
    assert await read_proxy_header(reader) == ("2001:db8::7", 40001)
    assert await reader.read() == b"HELLO"


async def test_ipv4_mapped_ipv6_counts_as_the_ipv4_address() -> None:
    assert await read_proxy_header(stream_of(proxy_header("::ffff:198.51.100.4", 5000))) == ("198.51.100.4", 5000)


@pytest.mark.parametrize("command, family", [(0x0, 0x11), (0x1, 0x00)])
async def test_local_and_unspecified_mean_the_connection_itself(command: int, family: int) -> None:
    header = PROXY_V2_SIGNATURE + struct.pack("!BBH", 0x20 | command, family, 0)
    assert await read_proxy_header(stream_of(header)) is None


@pytest.mark.parametrize(
    "data, problem",
    [
        (b"PROXY TCP4 192.0.2.1 127.0.0.1 56324 443\r\n", "v1 header"),
        (b"\x00\x00\x00\x2a{\"t\":\"HELLO\"}", "didn't start with a PROXY v2 header"),
        (IPV4_EXAMPLE[:20], "cut short"),
        (IPV4_EXAMPLE[:12] + b"\x11" + IPV4_EXAMPLE[13:], "version 1"),
        (IPV4_EXAMPLE[:12] + b"\x22" + IPV4_EXAMPLE[13:], "unknown command"),
        (IPV4_EXAMPLE[:13] + b"\x12" + IPV4_EXAMPLE[14:], "other than TCP"),
        (PROXY_V2_SIGNATURE + b"\x21\x11\x00\x04" + bytes(4), "addresses are cut short"),
    ],
)
async def test_bad_headers_are_refused(data: bytes, problem: str) -> None:
    with pytest.raises(ProxyHeaderError, match=problem):
        await read_proxy_header(stream_of(data))


async def test_remote_address_drops_a_caller_with_no_header(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="hearth.transport")
    answers: list[tuple[str, int] | None] = []

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        answers.append(await remote_address(reader, writer, proxy_protocol=False, timeout=1))
        answers.append(await remote_address(reader, writer, proxy_protocol=True, timeout=0.2))
        writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    _, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
    async with asyncio.timeout(5):
        while len(answers) < 2:
            await asyncio.sleep(0.01)
    writer.close()
    server.close()
    # Without --proxy-protocol it's the socket's address; with it, silence gets dropped.
    assert answers[0] is not None and answers[0][0] == "127.0.0.1" and answers[1] is None
    assert "no PROXY header in time" in caplog.text


# --- TLS ---------------------------------------------------------------------------------


async def test_tls_dial_sends_the_name_and_skips_the_certificate_check() -> None:
    names: list[str | None] = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(TEST_CERT)
    context.sni_callback = lambda _socket, name, _context: names.append(name)  # type: ignore[assignment,func-returns-value]

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.readexactly(5))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0, ssl=context)
    try:
        # "localhost" isn't the certificate's name, and nobody signed it: that's fine.
        reader, writer = await open_stream("localhost", server.sockets[0].getsockname()[1], tls=True)
        writer.write(b"HELLO")
        assert await reader.readexactly(5) == b"HELLO"
        assert writer.get_extra_info("ssl_object") is not None
        writer.close()
    finally:
        server.close()
    # Funnel routes by this name, so it must go out.
    assert names == ["localhost"]


# --- Addresses ---------------------------------------------------------------------------


def test_dial_addresses_with_and_without_tls() -> None:
    funnel = parse_dial_address("tls://hearth-vm.tail1234.ts.net:443", "ab" * 32)
    assert funnel == DialTarget("hearth-vm.tail1234.ts.net", 443, "ab" * 32, tls=True)
    assert funnel.text() == "tls://hearth-vm.tail1234.ts.net:443"
    plain = parse_dial_address("hearth.example.net:7777")
    assert plain == DialTarget("hearth.example.net", 7777) and plain.text() == "hearth.example.net:7777"


@pytest.mark.parametrize("text", ["tls://", "tls://:443", "tls://host", "https://host:443", "tls://a/b:443"])
def test_bad_dial_addresses_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_dial_address(text)

"""Tests for protocol.py: framing limits and strict message schemas."""

import asyncio

import pytest

from hearth import config, protocol
from hearth.protocol import FrameTooLarge, ProtocolError

PUBKEY = "ab" * 32


def reader_with(data: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


async def test_frame_round_trip() -> None:
    reader = reader_with(protocol.encode_frame(b"hello") + protocol.encode_frame(b""))
    assert await protocol.read_frame(reader) == b"hello"
    assert await protocol.read_frame(reader) == b""


async def test_oversized_header_is_refused_before_reading_the_payload() -> None:
    three_mib = 3 * 2**20
    # No payload at all after the header, and no EOF: if read_frame tried to
    # read the payload it would wait forever, and the timeout would fire instead.
    reader = reader_with(three_mib.to_bytes(4, "big"), eof=False)
    with pytest.raises(FrameTooLarge, match="moving truck"):
        async with asyncio.timeout(2):
            await protocol.read_frame(reader)


async def test_exactly_the_limit_is_allowed() -> None:
    header = config.MAX_FRAME_BYTES.to_bytes(4, "big")
    reader = reader_with(header + b"\0" * config.MAX_FRAME_BYTES)
    assert len(await protocol.read_frame(reader)) == config.MAX_FRAME_BYTES


async def test_truncated_frame_is_an_incomplete_read() -> None:
    reader = reader_with((10).to_bytes(4, "big") + b"short")
    with pytest.raises(asyncio.IncompleteReadError):
        await protocol.read_frame(reader)


def test_we_never_send_an_oversized_frame() -> None:
    with pytest.raises(FrameTooLarge):
        protocol.encode_frame(b"\0" * (config.MAX_FRAME_BYTES + 1))


def test_valid_messages_parse() -> None:
    hello = protocol.make_hello(PUBKEY, PUBKEY, PUBKEY)
    for message in (hello, protocol.make_auth("cd" * 64), protocol.make_ping(), protocol.make_pong(),
                    protocol.make_error("bad_frame", "nope")):
        assert protocol.parse_message(protocol.encode_message(message)) == message


@pytest.mark.parametrize(
    "payload, problem",
    [
        (b'{"t":"GOSSIP"}', "unknown message type"),
        (b'{"x":1}', "unknown message type"),
        (b'["PING"]', "not a JSON object"),
        (b'{"t":"PING","extra":1}', "wrong fields"),
        (b'{"t":"AUTH"}', "wrong fields"),
        (b'{"t":"AUTH","sig":"nothex"}', "AUTH.sig is invalid"),
        (b'{"t":"ERROR","code":"made_up","msg":"x"}', "ERROR.code is invalid"),
        (b'{"t":"PING","t":"PONG"}', "duplicate key"),
        (b'{"t":"PING"', "not strict JSON"),
        (b'{"t":1.5}', "floats"),
    ],
)
def test_bad_messages_are_refused(payload: bytes, problem: str) -> None:
    with pytest.raises(ProtocolError, match=problem):
        protocol.parse_message(payload)


def test_hello_with_uppercase_hex_is_refused() -> None:
    hello = protocol.make_hello(PUBKEY.upper(), PUBKEY, PUBKEY)
    with pytest.raises(ProtocolError, match="HELLO.pubkey"):
        protocol.parse_message(protocol.encode_message(hello))


def test_error_text_is_capped() -> None:
    long_error = protocol.make_error("internal", "x" * 10_000)
    assert len(long_error["msg"]) == config.MAX_ERROR_TEXT_CHARS
    too_long = b'{"t":"ERROR","code":"internal","msg":"' + b"x" * 501 + b'"}'
    with pytest.raises(ProtocolError, match="ERROR.msg"):
        protocol.parse_message(too_long)


def test_snippet_keeps_logs_short_and_single_line() -> None:
    text = protocol.snippet("line one\nline two" * 20)
    assert "\n" not in text and len(text) <= protocol.LOG_SNIPPET_CHARS + 1

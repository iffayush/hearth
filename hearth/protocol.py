"""The wire protocol: length-prefixed frames and strict message schemas.

A frame is a 4-byte big-endian length followed by that many payload bytes.
Frames over 2 MiB are refused before a single payload byte is read. During the
handshake the payload is plain JSON; afterwards `peer.py` encrypts it. A "length"
that is really the start of HTTP or TLS gets named, so the error can say what
actually called: a web crawler, or a link with the wrong port.

Every message is a JSON object with a "t" (type) field. `parse_message()`
checks it against the explicit schema below: known type, exactly the expected
fields, and every field the right type and format. Anything else raises
ProtocolError, and the caller decides whether that means "drop it" or "hang up".

Phase 5 knows the handshake, keepalive and ERROR messages, the log sync
messages (HEADS, WANT, ENTRIES), the chunk messages (CHUNK_WANT, CHUNK,
CHUNK_MISSING, CHUNK_PUSH, CHUNK_ACK) and the invite messages (INVITE_REDEEM,
INVITE_OK).

Joke: TCP gives you a firehose of bytes. Framing is the bit where we politely
insist on cups.
"""

import asyncio
import math
import re
from collections.abc import Callable
from typing import Any

from hearth import canonical, config

LENGTH_BYTES = 4
ERROR_CODES = ("bad_frame", "not_member", "bad_invite", "rate_limited", "quota_exceeded", "internal")
# Same format rules as entry.py: lowercase hex of an exact length.
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX128 = re.compile(r"[0-9a-f]{128}")
# Untrusted text echoed into log lines is cut to this length.
LOG_SNIPPET_CHARS = 40
# A whole encrypted chunk, base64-encoded: 4 characters per 3 bytes, rounded up.
MAX_CHUNK_BASE64_CHARS = 4 * math.ceil(config.MAX_CHUNK_CIPHERTEXT_BYTES / 3)
BASE64_CHARS = re.compile(r"[A-Za-z0-9+/]*={0,2}")
# An invite token: 32 random bytes as unpadded base64url, always 43 characters.
INVITE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
# A sealed group key: SealedBox adds 48 bytes to the 32-byte key; 80 bytes is 108 base64 characters.
SEALED_KEY_CHARS = 108
# How HTTP looks in the first 4 bytes: request methods, the HTTP/2 preface, and a reply's "HTTP/1.1".
HTTP_STARTS = (b"GET ", b"POST", b"HEAD", b"PUT ", b"DELE", b"OPTI", b"PATC", b"CONN", b"TRAC", b"PRI ", b"HTTP")
# A TLS record starts with its type (alert or handshake), then major version 3.
TLS_RECORD_TYPES = (0x15, 0x16)
TLS_MAJOR_VERSION = 3


class ProtocolError(Exception):
    """A frame or message breaks the protocol. The message says how."""


class FrameTooLarge(ProtocolError):
    """A frame header announced more than the frame limit."""


class ForeignProtocol(FrameTooLarge):
    """The oversized "length" is the start of another protocol. `speaks` is "HTTP" or "TLS"."""

    def __init__(self, speaks: str) -> None:
        super().__init__(f"this is {speaks}, not Hearth")
        self.speaks = speaks


# --- Framing -------------------------------------------------------------------


def encode_frame(payload: bytes) -> bytes:
    """Prefix `payload` with its length. Refuses to build a frame we'd reject ourselves."""
    if len(payload) > config.MAX_FRAME_BYTES:
        raise FrameTooLarge(f"refusing to send a {len(payload)}-byte frame")
    return len(payload).to_bytes(LENGTH_BYTES, "big") + payload


async def read_frame(reader: asyncio.StreamReader, max_bytes: int = config.MAX_FRAME_BYTES) -> bytes:
    """Read one frame's payload. Raises FrameTooLarge or asyncio.IncompleteReadError."""
    header = await reader.readexactly(LENGTH_BYTES)
    length = int.from_bytes(header, "big")
    if length > max_bytes:
        # Checked before reading the payload, so a liar can't make us buffer it.
        speaks = foreign_protocol(header)
        if speaks is not None:
            raise ForeignProtocol(speaks)
        raise FrameTooLarge(
            f"Frame too big ({length / 2**20:.1f} MiB > {max_bytes / 2**20:.0f} MiB). "
            "We're a forum, not a moving truck."
        )
    return await reader.readexactly(length)


def foreign_protocol(header: bytes) -> str | None:
    """What an impossibly long frame header really is: "HTTP", "TLS", or None if we can't tell."""
    if header in HTTP_STARTS:
        return "HTTP"
    if header[0] in TLS_RECORD_TYPES and header[1] == TLS_MAJOR_VERSION:
        return "TLS"
    return None


# --- Messages ------------------------------------------------------------------


def encode_message(message: dict[str, Any]) -> bytes:
    return canonical.dumps(message)


def parse_message(payload: bytes) -> dict[str, Any]:
    """Strictly parse and schema-check one message. Raises ProtocolError on any problem."""
    try:
        message = canonical.loads(payload)
    except canonical.CanonicalError as error:
        raise ProtocolError(f"message is not strict JSON: {error}") from error
    if not isinstance(message, dict):
        raise ProtocolError("message is not a JSON object")
    kind = message.get("t")
    if not isinstance(kind, str) or kind not in SCHEMAS:
        raise ProtocolError(f"unknown message type {snippet(kind)}")
    schema = SCHEMAS[kind]
    fields = set(message) - {"t"}
    if fields != set(schema):
        missing = sorted(set(schema) - fields)
        extra = [snippet(name) for name in sorted(fields - set(schema))]
        raise ProtocolError(f"{kind} has the wrong fields (missing {missing}, unexpected {extra})")
    for name, is_valid in schema.items():
        if not is_valid(message[name]):
            raise ProtocolError(f"{kind}.{name} is invalid")
    return message


def snippet(value: Any, limit: int = LOG_SNIPPET_CHARS) -> str:
    """A short, quoted, log-safe rendering of an untrusted value (repr escapes newlines)."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def make_hello(pubkey: str, eph: str, nonce: str) -> dict[str, Any]:
    return {"t": "HELLO", "proto": config.PROTOCOL_VERSION, "pubkey": pubkey, "eph": eph, "nonce": nonce}


def make_auth(sig: str) -> dict[str, Any]:
    return {"t": "AUTH", "sig": sig}


def make_ping() -> dict[str, Any]:
    return {"t": "PING"}


def make_pong() -> dict[str, Any]:
    return {"t": "PONG"}


def make_error(code: str, msg: str) -> dict[str, Any]:
    return {"t": "ERROR", "code": code, "msg": msg[: config.MAX_ERROR_TEXT_CHARS]}


def make_heads(heads: dict[str, tuple[int, str]]) -> dict[str, Any]:
    """HEADS: for every author we hold, [latest seq, hash of that entry]."""
    return {"t": "HEADS", "heads": {author: [seq, hash_] for author, (seq, hash_) in heads.items()}}


def make_want(author: str, first: int, last: int) -> dict[str, Any]:
    return {"t": "WANT", "author": author, "from": first, "to": last}


def make_entries(raws: list[str]) -> dict[str, Any]:
    return {"t": "ENTRIES", "entries": raws}


def make_chunk_want(chunk_ids: list[str]) -> dict[str, Any]:
    return {"t": "CHUNK_WANT", "ids": chunk_ids}


def make_chunk(chunk_id: str, data: str) -> dict[str, Any]:
    """`data` is the encrypted chunk, base64-encoded."""
    return {"t": "CHUNK", "id": chunk_id, "data": data}


def make_chunk_missing(chunk_ids: list[str]) -> dict[str, Any]:
    return {"t": "CHUNK_MISSING", "ids": chunk_ids}


def make_chunk_push(chunk_id: str, data: str) -> dict[str, Any]:
    return {"t": "CHUNK_PUSH", "id": chunk_id, "data": data}


def make_chunk_ack(chunk_id: str) -> dict[str, Any]:
    return {"t": "CHUNK_ACK", "id": chunk_id}


def make_invite_redeem(token: str, enc_pubkey: str) -> dict[str, Any]:
    """`enc_pubkey` is the joiner's X25519 key: where to seal the group key."""
    return {"t": "INVITE_REDEEM", "token": token, "enc_pubkey": enc_pubkey}


def make_invite_ok(group: str, admin: str, sealed_key: str) -> dict[str, Any]:
    return {"t": "INVITE_OK", "group": group, "admin": admin, "sealed_key": sealed_key}


# --- Field validators ----------------------------------------------------------


def is_hex64(value: Any) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def is_hex128(value: Any) -> bool:
    return isinstance(value, str) and HEX128.fullmatch(value) is not None


def is_proto(value: Any) -> bool:
    # Any short string: a version mismatch is handled by the handshake, with a clear reason.
    return isinstance(value, str) and len(value) <= 32


def is_error_code(value: Any) -> bool:
    return isinstance(value, str) and value in ERROR_CODES


def is_error_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= config.MAX_ERROR_TEXT_CHARS


def is_seq(value: Any) -> bool:
    return canonical.is_int(value) and 1 <= value <= config.MAX_SEQ


def is_heads(value: Any) -> bool:
    """{author hex: [seq, entry hash hex]} for any number of authors (the frame limit caps it)."""
    if not isinstance(value, dict):
        return False
    for author, head in value.items():
        if not is_hex64(author):
            return False
        if not (isinstance(head, list) and len(head) == 2 and is_seq(head[0]) and is_hex64(head[1])):
            return False
    return True


def is_chunk_id_list(value: Any) -> bool:
    """1 to 4 distinct chunk IDs."""
    return (
        isinstance(value, list)
        and 1 <= len(value) <= config.MAX_CHUNK_WANT
        and len(set(value)) == len(value)
        and all(is_hex64(chunk_id) for chunk_id in value)
    )


def is_chunk_data(value: Any) -> bool:
    """Base64 text no longer than one encrypted chunk. The hash check happens on receipt."""
    return (
        isinstance(value, str)
        and len(value) <= MAX_CHUNK_BASE64_CHARS
        and BASE64_CHARS.fullmatch(value) is not None
    )


def is_invite_token(value: Any) -> bool:
    return isinstance(value, str) and INVITE_TOKEN.fullmatch(value) is not None


def is_sealed_key(value: Any) -> bool:
    return isinstance(value, str) and len(value) == SEALED_KEY_CHARS and BASE64_CHARS.fullmatch(value) is not None


def is_raw_entry_list(value: Any) -> bool:
    """Up to one WANT range of raw entries, each a string no longer than the entry limit.

    The store parses and checks every entry properly; this only bounds the sizes.
    """
    if not isinstance(value, list) or len(value) > config.MAX_WANT_RANGE:
        return False
    return all(isinstance(raw, str) and len(raw) <= config.MAX_ENTRY_BYTES for raw in value)


# Every message type, and a validator for each of its fields (besides "t").
SCHEMAS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "HELLO": {"proto": is_proto, "pubkey": is_hex64, "eph": is_hex64, "nonce": is_hex64},
    "AUTH": {"sig": is_hex128},
    "PING": {},
    "PONG": {},
    "ERROR": {"code": is_error_code, "msg": is_error_text},
    "HEADS": {"heads": is_heads},
    "WANT": {"author": is_hex64, "from": is_seq, "to": is_seq},
    "ENTRIES": {"entries": is_raw_entry_list},
    "CHUNK_WANT": {"ids": is_chunk_id_list},
    "CHUNK": {"id": is_hex64, "data": is_chunk_data},
    "CHUNK_MISSING": {"ids": is_chunk_id_list},
    "CHUNK_PUSH": {"id": is_hex64, "data": is_chunk_data},
    "CHUNK_ACK": {"id": is_hex64},
    "INVITE_REDEEM": {"token": is_invite_token, "enc_pubkey": is_hex64},
    "INVITE_OK": {"group": is_hex64, "admin": is_hex64, "sealed_key": is_sealed_key},
}

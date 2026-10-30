"""The cryptography of one connection: what the handshake signs, and the session it yields.

**What the handshake signs.** Both sides send AUTH: an Ed25519 signature over

    b"hearth/handshake/v1\\n" + initiator pubkey + eph + nonce + responder pubkey + eph + nonce

(the six HELLO values as hex strings, concatenated). See `handshake_transcript`.

**Session keys.** Both sides derive them from the ephemeral X25519 keys in HELLO:

    initiator: rx, tx = crypto_kx_client_session_keys(own_eph_pk, own_eph_sk, peer_eph_pk)
    responder: rx, tx = crypto_kx_server_session_keys(own_eph_pk, own_eph_sk, peer_eph_pk)

Both send with tx and receive with rx, so the initiator's tx equals the
responder's rx (and the other way round). test_peer.py checks this mapping.

**The session.** Every frame after the handshake is SecretBox(tx).encrypt(json,
nonce) without the nonce: the nonce is a per-direction frame counter (0, 1, 2 …
as 24 bytes big-endian) that both ends track. The receiver advances its counter
for every frame *before* trying to decrypt, so a tampered, replayed or
reordered frame fails to decrypt (and peer.py hangs up), while one that
decrypts to junk JSON is merely dropped.

The plaintext half of the handshake (sending and reading HELLO and AUTH, and
the checks on a HELLO) is here too, along with PeerClosed, which ends any
connection with a reason.

Joke: two strangers, one shared secret, zero awkward small talk.
"""

import asyncio
from typing import Any

from nacl import bindings
from nacl.secret import SecretBox

from hearth import config, protocol


class PeerClosed(Exception):
    """Ends a connection. If `error_code` is set and a session exists, an ERROR is sent first.

    `suspicious` marks closes that hint at an attack or a broken peer (forged
    signatures, tampering, floods): they're logged as warnings, not routine info.
    `not_hearth` means the other side spoke some other protocol altogether.
    """

    def __init__(
        self, reason: str, error_code: str | None = None, suspicious: bool = False, not_hearth: bool = False
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_code = error_code
        self.suspicious = suspicious
        self.not_hearth = not_hearth


class Session:
    """Per-direction keys and frame counters for one established connection."""

    def __init__(self, tx_key: bytes, rx_key: bytes) -> None:
        self._tx_box = SecretBox(tx_key)
        self._rx_box = SecretBox(rx_key)
        self.tx_counter = 0
        self.rx_counter = 0

    def seal(self, plaintext: bytes) -> bytes:
        """Encrypt the next outgoing frame. The nonce is implied by the counter, never sent."""
        nonce = counter_nonce(self.tx_counter)
        self.tx_counter += 1
        return self._tx_box.encrypt(plaintext, nonce).ciphertext

    def open(self, ciphertext: bytes) -> bytes:
        """Decrypt the next incoming frame. Raises CryptoError if it isn't the one we expect."""
        nonce = counter_nonce(self.rx_counter)
        # Advance before decrypting: a frame that fails still used up its number.
        self.rx_counter += 1
        return self._rx_box.decrypt(ciphertext, nonce)


def counter_nonce(counter: int) -> bytes:
    return counter.to_bytes(SecretBox.NONCE_SIZE, "big")


def handshake_transcript(initiator_hello: dict[str, Any], responder_hello: dict[str, Any]) -> bytes:
    """The bytes both sides sign in AUTH.

    Plain concatenation is unambiguous ONLY because all six values are exactly
    64 hex characters (protocol.is_hex64 checked them). Don't turn this into a
    join of variable-length fields without adding separators or lengths.
    """
    parts = []
    for hello in (initiator_hello, responder_hello):
        for name in ("pubkey", "eph", "nonce"):
            if not protocol.is_hex64(hello[name]):
                raise ValueError(f"handshake field {name} must be exactly 64 hex chars")
            parts.append(hello[name])
    return config.HANDSHAKE_SIGN_PREFIX + "".join(parts).encode("ascii")


def derive_session_keys(
    initiator: bool, own_eph_pk: bytes, own_eph_sk: bytes, peer_eph_pk: bytes
) -> tuple[bytes, bytes]:
    """Return (rx, tx) for our side. See the module docstring for the mapping."""
    if initiator:
        return bindings.crypto_kx_client_session_keys(own_eph_pk, own_eph_sk, peer_eph_pk)
    return bindings.crypto_kx_server_session_keys(own_eph_pk, own_eph_sk, peer_eph_pk)


async def send_plain(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    """Send a plaintext handshake message (HELLO or AUTH)."""
    writer.write(protocol.encode_frame(protocol.encode_message(message)))
    await writer.drain()


def not_hearth_close(speaks: str, who: str, dialled: bool) -> PeerClosed:
    """The close for a caller, or an address we dialled, that speaks HTTP or TLS instead of Hearth."""
    if dialled:
        # Our own settings or link point at the wrong place, and only a person can fix that.
        if speaks == "TLS":
            reason = f"{who} answered with TLS, not Hearth; the address probably needs to start with tls://"
        else:
            reason = f"{who} answered like a web server, not a Hearth node; check the port in the address"
        return PeerClosed(reason, suspicious=True, not_hearth=True)
    # Strangers do this all day once an address is public, so it's no reason to raise an alarm.
    if speaks == "TLS":
        reason = "a TLS connection, but this node doesn't use TLS (a scanner, or a link with tls:// by mistake)"
    else:
        reason = "a web request, not Hearth (probably a crawler that found this address; harmless)"
    return PeerClosed(reason, not_hearth=True)


async def read_plain(
    reader: asyncio.StreamReader, expected_type: str, who: str, dialled: bool = False
) -> dict[str, Any]:
    """Read one plaintext handshake message; anything unexpected ends the connection."""
    try:
        message = protocol.parse_message(await protocol.read_frame(reader))
    except protocol.ForeignProtocol as error:
        raise not_hearth_close(error.speaks, who, dialled) from error
    except protocol.ProtocolError as error:
        raise PeerClosed(f"bad handshake frame from {who}: {error}", suspicious=True) from error
    if message["t"] != expected_type:
        raise PeerClosed(f"expected {expected_type} from {who}, got {message['t']}")
    return message


def check_hello(hello: dict[str, Any], own_pubkey: str, expected_pubkey: str | None, address: str) -> None:
    """Refuse a HELLO from the wrong protocol, from ourselves, or from anyone but a pinned key."""
    if hello["proto"] != config.PROTOCOL_VERSION:
        raise PeerClosed(f"peer speaks {protocol.snippet(hello['proto'])}, we speak {config.PROTOCOL_VERSION}")
    if hello["pubkey"] == own_pubkey:
        # Both sides sign the same transcript, so without this check someone
        # could bounce our own AUTH back at us and "log in" as us.
        raise PeerClosed("peer claims to be us; refusing to talk to a mirror", suspicious=True)
    if expected_pubkey is not None and hello["pubkey"] != expected_pubkey:
        raise PeerClosed(
            f"{hello['pubkey'][:8]} at {address} doesn't match the pinned key {expected_pubkey[:8]}; aborting",
            suspicious=True,
        )

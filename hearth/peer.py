"""One connection to another node: handshake, encrypted session, state machine.

Both ends run this same code. The *initiator* is whichever side dialled. The
cryptography (what AUTH signs, the session keys, the counter-nonce session)
lives in `session.py`; this file is the conversation around it.

**Handshake.** Both send HELLO (pubkey, fresh ephemeral key, random nonce),
then AUTH (a signature over both HELLOs), then derive the session keys; a
pinned key (`expected_pubkey`) is checked as soon as HELLO arrives.

**States** (the full two-sided table is in docs/phase-6.md):

    AWAIT_HELLO -> AWAIT_AUTH -> ACTIVE   the peer is a member (the authorizer says so)
                              -> LIMITED  home node only: an unknown peer, who may
                                          only send INVITE_REDEEM; nothing is sent
                                          to it unprompted (no HEADS, no PING)
                              -> JOINING  joiner only: we sent INVITE_REDEEM and
                                          wait (15 s) for INVITE_OK, nothing else
    any state -> CLOSED

Identity (who is this? the handshake signature, plus a pinned key) and
authorization (what may they do? the `authorizer`) are separate questions.

**Hooks.** Peer handles the handshake, PING/PONG and ERROR itself. The rest is
the owner's business: `on_active` runs on entering ACTIVE (sync sends HEADS),
`on_message` gets every other ACTIVE message, `on_closed` runs at the end,
`on_invite` (home node) answers an INVITE_REDEEM, and `on_joined` (joiner)
saves the group key once INVITE_OK checks out.

Joke: a handshake where both sides show ID, swap secret decoder rings, then
burn the instructions. Spies would be proud; HR would have questions.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nacl import bindings
from nacl.exceptions import CryptoError
from nacl.utils import random as random_bytes

from hearth import config, protocol
from hearth.identity import Identity, verify_signature
from hearth.invite import InviteError, InviteLink, open_invite_ok
from hearth.session import (
    PeerClosed, Session, check_hello, derive_session_keys, handshake_transcript, read_plain, send_plain,
)

logger = logging.getLogger(__name__)

AWAIT_HELLO = "AWAIT_HELLO"
AWAIT_AUTH = "AWAIT_AUTH"
ACTIVE = "ACTIVE"
LIMITED = "LIMITED"
JOINING = "JOINING"
CLOSED = "CLOSED"

HANDSHAKE_NONCE_BYTES = 32

# Hook types: see "Hooks" in the module docstring.
ActiveHook = Callable[["Peer"], Awaitable[None]]
MessageHook = Callable[["Peer", dict[str, Any]], Awaitable[None]]
ClosedHook = Callable[["Peer"], None]
InviteHook = Callable[["Peer", dict[str, Any]], Awaitable[dict[str, Any]]]
JoinedHook = Callable[["Peer", str, str, bytes], Awaitable[None]]


class Peer:
    """Runs one connection from handshake to close. Create it, then `await run()`."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        identity: Identity,
        initiator: bool,
        authorizer: Callable[[str], bool],
        expected_pubkey: str | None = None,
        invite: InviteLink | None = None,
        handshake_timeout: float = config.HANDSHAKE_TIMEOUT,
        idle_timeout: float = config.IDLE_TIMEOUT,
        ping_interval: float = config.PING_INTERVAL,
        join_timeout: float = config.JOIN_TIMEOUT,
        on_active: ActiveHook | None = None,
        on_message: MessageHook | None = None,
        on_closed: ClosedHook | None = None,
        on_invite: InviteHook | None = None,
        on_joined: JoinedHook | None = None,
        remote: tuple[str, int] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._identity = identity
        self.initiator = initiator
        self._authorizer = authorizer
        # When dialling the home node we know exactly whose key to expect.
        self.expected_pubkey = expected_pubkey
        # Set only on a joiner's connection: the invite it's redeeming.
        self._invite = invite
        self._handshake_timeout = handshake_timeout
        self._idle_timeout = idle_timeout
        self._ping_interval = ping_interval
        self._join_timeout = join_timeout
        self._on_active = on_active
        self._on_message = on_message
        self._on_closed = on_closed
        self._on_invite = on_invite
        self._on_joined = on_joined

        # `remote` is the caller as a front door's PROXY header names them (transport.py).
        peername = remote or writer.get_extra_info("peername")
        self.remote_ip = peername[0] if peername else "unknown"
        self.address = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        self.state = AWAIT_HELLO
        self.peer_pubkey: str | None = None
        self.close_reason: str | None = None
        self.pongs_received = 0
        # The code of the ERROR the peer sent before hanging up, if it did (e.g. "quota_exceeded").
        self.error_received: str | None = None
        # The other side spoke HTTP or TLS, so retrying the same address can't help.
        self.not_hearth = False
        self.became_active = asyncio.Event()
        self._session: Session | None = None
        self._ping_task: asyncio.Task[None] | None = None
        # Raised to WARNING for suspicious closes (see PeerClosed).
        self._close_level = logging.INFO

    def label(self) -> str:
        """How this peer appears in logs: short pubkey once known, else the address."""
        return self.peer_pubkey[:8] if self.peer_pubkey else self.address

    async def run(self) -> None:
        """Handshake, then handle messages until the connection ends. Never raises
        (except cancellation): one misbehaving peer must never take the node down."""
        try:
            await self._handshake()
            await self._message_loop()
        except PeerClosed as closed:
            self.close_reason = closed.reason
            self._close_level = logging.WARNING if closed.suspicious else logging.INFO
            self.not_hearth = closed.not_hearth
            await self._send_error_quietly(closed.error_code, closed.reason)
        except (asyncio.IncompleteReadError, ConnectionError):
            self.close_reason = "the other side closed the connection"
        except asyncio.CancelledError:
            self.close_reason = "shutting down"
            raise
        except Exception:
            logger.exception("Unexpected error on the connection with %s; closing it", self.label())
            self.close_reason = "internal error"
            await self._send_error_quietly("internal", "internal error")
        finally:
            await self._close()

    async def send(self, message: dict[str, Any]) -> None:
        """Encrypt and send one message. Only valid once the session exists."""
        if self._session is None:
            raise RuntimeError("no session yet: only HELLO and AUTH go out in plaintext")
        # seal() and write() run with no await between them, so two tasks
        # sending at once can never put frames on the wire out of counter order.
        payload = self._session.seal(protocol.encode_message(message))
        self._writer.write(protocol.encode_frame(payload))
        await self._writer.drain()

    # --- Handshake ---------------------------------------------------------

    async def _handshake(self) -> None:
        try:
            async with asyncio.timeout(self._handshake_timeout):
                await self._run_handshake()
        except TimeoutError as error:
            raise PeerClosed(f"handshake with {self.label()} timed out") from error
        # Outside the timeout on purpose: what happens next (sending HEADS,
        # uploading an outbox, redeeming an invite) isn't the handshake's work.
        if self.peer_pubkey is None:
            raise RuntimeError("the handshake finished without learning the peer's key")
        if self._invite is not None:
            # A joiner trusts only the pinned key from the link (checked at HELLO).
            await self._enter_joining()
        elif self._authorizer(self.peer_pubkey):
            await self._enter_active()
        elif self._on_invite is not None:
            self._enter_limited()
        else:
            raise PeerClosed(f"{self.label()} isn't a member of this group", "not_member")

    async def _run_handshake(self) -> None:
        eph_pk, eph_sk = bindings.crypto_kx_keypair()
        own_hello = protocol.make_hello(self._identity.pubkey, eph_pk.hex(), random_bytes(HANDSHAKE_NONCE_BYTES).hex())
        await send_plain(self._writer, own_hello)
        their_hello = await read_plain(self._reader, "HELLO", self.label(), self.initiator)
        check_hello(their_hello, self._identity.pubkey, self.expected_pubkey, self.address)
        self.peer_pubkey = their_hello["pubkey"]
        self.state = AWAIT_AUTH

        if self.initiator:
            transcript = handshake_transcript(own_hello, their_hello)
        else:
            transcript = handshake_transcript(their_hello, own_hello)
        await send_plain(self._writer, protocol.make_auth(self._identity.sign(transcript)))
        their_auth = await read_plain(self._reader, "AUTH", self.label(), self.initiator)
        if not verify_signature(self.peer_pubkey, transcript, their_auth["sig"]):
            raise PeerClosed(f"{self.label()} sent a bad handshake signature. Nice try.", suspicious=True)

        try:
            rx, tx = derive_session_keys(self.initiator, eph_pk, eph_sk, bytes.fromhex(their_hello["eph"]))
        except CryptoError as error:
            # libsodium refuses degenerate (low-order) ephemeral keys.
            raise PeerClosed(f"{self.label()} sent an unusable ephemeral key", suspicious=True) from error
        # Forward secrecy: once this name is gone the key can't be used again.
        # (Python can't wipe memory, but nothing keeps a reference to it.)
        del eph_sk
        self._session = Session(tx_key=tx, rx_key=rx)

    # --- After the handshake ---------------------------------------------------

    async def _enter_active(self) -> None:
        self.state = ACTIVE
        self.became_active.set()
        logger.info("Handshake complete with %s (%s); session encrypted", self.label(), self.address)
        self._ping_task = asyncio.create_task(self._ping_loop())
        if self._on_active is not None:
            await self._on_active(self)

    def _enter_limited(self) -> None:
        # Nothing goes out unprompted: no HEADS, no PING. Only an invite gets them in.
        self.state = LIMITED
        logger.info("%s (%s) isn't a member; it may only redeem an invite", self.label(), self.address)

    async def _enter_joining(self) -> None:
        if self._invite is None:
            raise RuntimeError("JOINING needs an invite")
        self.state = JOINING
        logger.info("Connected to the home node %s; redeeming the invite", self.label())
        await self.send(protocol.make_invite_redeem(self._invite.token, self._identity.encryption_pubkey))

    async def _message_loop(self) -> None:
        while True:
            # A joiner waits 15 s for INVITE_OK; everyone else, the idle timeout.
            timeout = self._join_timeout if self.state == JOINING else self._idle_timeout
            try:
                async with asyncio.timeout(timeout):
                    payload = await protocol.read_frame(self._reader)
            except TimeoutError as error:
                raise PeerClosed(f"no word from {self.label()} in {timeout:g} s; hanging up") from error
            except protocol.FrameTooLarge as error:
                raise PeerClosed(f"{self.label()}: {error}", "bad_frame", suspicious=True) from error
            message = self._decrypt(payload)
            if message is None:
                continue
            if self.state == LIMITED:
                await self._handle_limited(message)
            elif self.state == JOINING:
                await self._handle_joining(message)
            else:
                await self._handle(message)

    def _decrypt(self, payload: bytes) -> dict[str, Any] | None:
        """Decrypt and parse one frame. Bad crypto closes; bad JSON is dropped."""
        if self._session is None:
            raise RuntimeError("_decrypt called before the handshake finished")
        try:
            plaintext = self._session.open(payload)
        except CryptoError as error:
            raise PeerClosed(
                f"frame #{self._session.rx_counter - 1} from {self.label()} failed to decrypt "
                "(tampered, replayed or out of order)",
                "bad_frame",
                suspicious=True,
            ) from error
        try:
            return protocol.parse_message(plaintext)
        except protocol.ProtocolError as error:
            logger.warning("Dropped a message from %s: %s", self.label(), error)
            return None

    async def _handle_limited(self, message: dict[str, Any]) -> None:
        """Home node, unknown peer: an INVITE_REDEEM is the only thing we'll hear."""
        if message["t"] != "INVITE_REDEEM" or self._on_invite is None:
            raise PeerClosed(f"{self.label()} isn't a member and sent {message['t']}", "not_member", suspicious=True)
        # on_invite raises PeerClosed (bad_invite, rate_limited) if the invite doesn't check out.
        await self.send(await self._on_invite(self, message))
        logger.info("%s redeemed an invite and is now a member. Welcome!", self.label())
        await self._enter_active()

    async def _handle_joining(self, message: dict[str, Any]) -> None:
        """Joiner: only INVITE_OK moves us on; anything else ends the attempt, saving nothing."""
        kind = message["t"]
        if kind == "INVITE_OK" and self._invite is not None and self._on_joined is not None:
            try:
                group_key = open_invite_ok(message, self._invite, self._identity)
            except InviteError as error:
                raise PeerClosed(f"rejected INVITE_OK from {self.label()}: {error}; nothing was saved",
                                 suspicious=True) from error
            await self._on_joined(self, message["group"], message["admin"], group_key)
            await self._enter_active()
        elif kind == "ERROR":
            self.error_received = message["code"]
            # The whole reason, since the person joining needs to read it (still escaped, though).
            reason = protocol.snippet(message["msg"], config.MAX_ERROR_TEXT_CHARS)
            raise PeerClosed(f"the home node refused: ERROR {message['code']}: {reason}")
        elif kind in ("HEADS", "PING"):
            # Only a member gets these, so the home node already counts us as one,
            # yet we never got the group key: an earlier join's INVITE_OK was lost.
            raise PeerClosed(
                "the home node already counts this identity as a member, but it never received the "
                "group key (an earlier join's reply was lost). Ask the admin for a new invite and "
                "join with a fresh profile."
            )
        else:
            raise PeerClosed(f"expected INVITE_OK from {self.label()}, got {kind}")

    async def _handle(self, message: dict[str, Any]) -> None:
        kind = message["t"]
        if kind == "PING":
            await self.send(protocol.make_pong())
        elif kind == "PONG":
            self._count_pong()
        elif kind == "ERROR":
            self.error_received = message["code"]
            raise PeerClosed(f"{self.label()} sent ERROR {message['code']}: {protocol.snippet(message['msg'])}")
        elif kind in ("HELLO", "AUTH", "INVITE_REDEEM", "INVITE_OK") or self._on_message is None:
            logger.info("Dropped %s from %s: not expected now", kind, self.label())
        else:
            await self._on_message(self, message)

    def _count_pong(self) -> None:
        self.pongs_received += 1
        if self.pongs_received == 1:
            logger.info("PONG from %s: the line is alive", self.label())
        elif self.pongs_received == config.PONG_MILESTONE:
            logger.info("%d PONGs from %s! Somebody get these two a cake.", self.pongs_received, self.label())
        else:
            logger.debug("PONG #%d from %s", self.pongs_received, self.label())

    async def _ping_loop(self) -> None:
        # The first PING goes out at once, so a fresh connection proves itself immediately.
        try:
            while True:
                await self.send(protocol.make_ping())
                await asyncio.sleep(self._ping_interval)
        except (ConnectionError, RuntimeError):
            # The read loop notices the dead connection and does the cleanup.
            return

    # --- Closing -----------------------------------------------------------

    async def _send_error_quietly(self, code: str | None, reason: str) -> None:
        """Tell the peer why we're hanging up, if there's an encrypted channel to say it on."""
        if code is None or self._session is None:
            return
        try:
            # Bounded, like _close(): a peer that stopped reading gets no last word.
            async with asyncio.timeout(self._idle_timeout):
                await self.send(protocol.make_error(code, reason))
        except (TimeoutError, ConnectionError, RuntimeError):
            pass

    async def _close(self) -> None:
        was_active = self.state == ACTIVE
        self.state = CLOSED
        if self._ping_task is not None:
            self._ping_task.cancel()
        if was_active and self._on_closed is not None:
            self._on_closed(self)
        self._writer.close()
        try:
            # Bounded: a peer that stopped reading can't hold the close open forever.
            async with asyncio.timeout(self._idle_timeout):
                await self._writer.wait_closed()
        except (TimeoutError, ConnectionError, OSError):
            self._writer.transport.abort()
        logger.log(self._close_level, "Closed connection with %s: %s", self.label(), self.close_reason)

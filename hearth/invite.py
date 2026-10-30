"""Invite links: create them on the home node, redeem them, and check the answer as a joiner.

The link carries everything a friend's node needs, and nothing secret but the token:

    hearth://join?h=<hostname>:<port>&k=<home pubkey>&g=<group id>&t=<token>

`h` may also be `tls://<hostname>:<port>`: the home node sits behind a front
door that only lets TLS through, such as Tailscale Funnel (transport.py).

1. `create_invite()` makes a random 32-byte token and stores only its BLAKE2b
   hash, with an expiry (48 h by default).
2. The joiner dials `h`, accepts only the node whose key is `k`, and sends
   INVITE_REDEEM with the token and its X25519 public key.
3. `redeem()` (home node) checks the token, writes the admin's `member_add`
   for the joiner, seals the group key to the joiner's X25519 key, and keeps
   the sealed key: all in one transaction (Store.redeem_invite). A token works
   once; a second use, by anyone, is refused.
4. `open_invite_ok()` (joiner) checks the reply is for group `g` from admin
   `k`, and that the key unseals, before anything is saved.

Joke: like a wedding invitation, except the RSVP is cryptographically signed
and the seating chart is an append-only log.
"""

import base64
import binascii
import hashlib
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox

from hearth import config, protocol
from hearth.entry import create_entry, is_hex64
from hearth.identity import Identity
from hearth.network import TLS_PREFIX, parse_dial_address
from hearth.store import Store, StoreError

LINK_FIELDS = frozenset({"h", "k", "g", "t"})
DURATION = re.compile(r"(\d+)([smhd])")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class InviteError(Exception):
    """An invite that can't be made, used, or trusted. The message says why."""


@dataclass
class InviteLink:
    """The parts of an invite link. `token` stays in its base64url text form."""

    host: str
    port: int
    home_pubkey: str
    group: str
    token: str
    # Dial the home node inside TLS (an `h=tls://...` link).
    tls: bool = False

    def address(self) -> str:
        """Where the home node is, as config.json keeps it: "host:port" or "tls://host:port"."""
        return f"{TLS_PREFIX if self.tls else ''}{self.host}:{self.port}"

    def text(self) -> str:
        return f"hearth://join?h={self.address()}&k={self.home_pubkey}&g={self.group}&t={self.token}"


def parse_link(text: str) -> InviteLink:
    """Strictly parse a link. Raises InviteError saying what's wrong with it."""
    parts = urlsplit(text.strip())
    if parts.scheme != "hearth" or parts.netloc != "join" or parts.path not in ("", "/"):
        raise InviteError("that isn't a Hearth invite link (it should start with hearth://join?)")
    try:
        fields = parse_qs(parts.query, strict_parsing=True)
    except ValueError as error:
        raise InviteError("the invite link's fields are garbled") from error
    if set(fields) != LINK_FIELDS or any(len(values) != 1 for values in fields.values()):
        raise InviteError("the invite link needs exactly one each of h, k, g and t")
    try:
        home = parse_dial_address(fields["h"][0])
    except ValueError as error:
        raise InviteError("the invite link's h= isn't host:port") from error
    link = InviteLink(home.host, home.port, fields["k"][0], fields["g"][0], fields["t"][0], home.tls)
    if not is_hex64(link.home_pubkey) or not is_hex64(link.group):
        raise InviteError("the invite link's k= or g= isn't 64 lowercase hex characters")
    if not protocol.is_invite_token(link.token):
        raise InviteError("the invite link's token is malformed (was the link cut short?)")
    return link


def parse_duration(text: str) -> int:
    """'48h' -> 172800. Units: s, m, h, d."""
    match = DURATION.fullmatch(text.strip())
    if match is None or int(match.group(1)) == 0:
        raise InviteError("give a duration like 30m, 48h or 7d")
    return int(match.group(1)) * DURATION_UNITS[match.group(2)]


def token_hash(token: str) -> str:
    """What the home node stores instead of the token itself."""
    return hashlib.blake2b(token.encode("ascii"), digest_size=32).hexdigest()


def create_invite(
    store: Store, admin: Identity, host: str, port: int, expires_in: int, now_ms: int, tls: bool = False
) -> InviteLink:
    """Make a single-use invite. Only the admin (the home node) can. `tls`: friends dial inside TLS."""
    group_id = store.group_id()
    if group_id is None or store.admin() != admin.pubkey:
        raise InviteError("only the group's admin (the home node) can create invites")
    token = base64.urlsafe_b64encode(secrets.token_bytes(config.INVITE_TOKEN_BYTES)).decode("ascii").rstrip("=")
    store.invites.add(token_hash(token), now_ms, now_ms + expires_in * 1000)
    return InviteLink(host, port, admin.pubkey, group_id, token, tls)


def redeem(
    store: Store, admin: Identity, group_key: bytes, token: str, joiner: str, enc_pubkey: str, now_ms: int
) -> dict[str, Any]:
    """Home node: turn a valid token into a member_add and an INVITE_OK. Raises InviteError."""
    group_id = store.group_id()
    if group_id is None or store.admin() != admin.pubkey:
        raise InviteError("this node isn't the group's admin, so it can't accept invites")
    hashed = token_hash(token)
    row = store.invites.get(hashed)
    if row is None:
        raise InviteError("unknown invite")
    if row.used_by is not None:
        raise InviteError("that invite has already been used")
    if row.expires < now_ms:
        raise InviteError("that invite has expired")
    member_add = create_entry(admin, group_id, store.head(admin.pubkey), "member_add", {"member": joiner}, now_ms)
    try:
        sealed = SealedBox(PublicKey(bytes.fromhex(enc_pubkey))).encrypt(group_key)
    except (ValueError, CryptoError) as error:
        raise InviteError("the joiner's encryption key is unusable") from error
    sealed_text = base64.b64encode(bytes(sealed)).decode("ascii")
    try:
        store.redeem_invite(hashed, joiner, member_add.raw(), sealed_text)
    except StoreError as error:
        raise InviteError(str(error)) from error
    return protocol.make_invite_ok(group_id, admin.pubkey, sealed_text)


def open_invite_ok(message: dict[str, Any], link: InviteLink, identity: Identity) -> bytes:
    """Joiner: check INVITE_OK against the link and unseal the group key. Raises InviteError."""
    if message["group"] != link.group:
        raise InviteError("it's for a different group than the link")
    if message["admin"] != link.home_pubkey:
        raise InviteError("it names a different admin than the link")
    try:
        group_key = SealedBox(identity.encryption_key).decrypt(base64.b64decode(message["sealed_key"], validate=True))
    except (binascii.Error, ValueError, CryptoError) as error:
        raise InviteError("the group key won't unseal") from error
    if len(group_key) != config.GROUP_KEY_BYTES:
        raise InviteError("the sealed group key is the wrong size")
    return group_key

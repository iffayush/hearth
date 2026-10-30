"""Log entries: the Entry dataclass, signing, and the pure validate_entry().

Every action in Hearth (creating the group, adding a member, posting) is an
entry appended to its author's own log. Each entry names the hash of the
author's previous entry in `prev`, so one author's entries form a hash chain.

    signed bytes = b"hearth/entry/v1\\n" + canonical JSON of every field but "sig"
    entry hash   = BLAKE2b-256(signed bytes)
    sig          = Ed25519 signature over the signed bytes

validate_entry() never touches a database: the caller looks up the author's
current Head and the member set and passes them in. That keeps every rule in
one place and makes each one testable with plain values.

Joke: the hash chain is like page numbers in a diary glued with superglue:
tear one out and everyone notices.
"""

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

from hearth import canonical, config
from hearth.identity import Identity, verify_signature

ENTRY_FIELDS = frozenset({"v", "group", "author", "seq", "prev", "ts", "kind", "body", "sig"})
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX128 = re.compile(r"[0-9a-f]{128}")
# A SecretBox box is at least a 24-byte nonce plus a 16-byte MAC.
MIN_BOX_BYTES = 40


@dataclass
class Head:
    """How far we have one author's log: their latest seq and its entry hash."""

    seq: int
    hash: str


@dataclass
class Reason:
    """Why an entry was rejected. `code` is for programs, `message` for humans."""

    code: str
    message: str


@dataclass
class Entry:
    """One signed log entry. Field meanings are in the spec's "Log entry" table."""

    v: int
    group: str
    author: str
    seq: int
    prev: str | None
    ts: int
    kind: str
    body: dict[str, Any]
    sig: str

    def to_dict(self) -> dict[str, Any]:
        """All fields, including sig, as a plain dict."""
        return {
            "v": self.v,
            "group": self.group,
            "author": self.author,
            "seq": self.seq,
            "prev": self.prev,
            "ts": self.ts,
            "kind": self.kind,
            "body": self.body,
            "sig": self.sig,
        }

    def raw(self) -> bytes:
        """Canonical JSON of the whole entry: what gets stored and sent."""
        return canonical.dumps(self.to_dict())

    def compute_hash(self) -> str:
        """This entry's hash; the next entry in the chain puts it in `prev`."""
        return entry_hash(self.to_dict())

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Entry":
        """Build an Entry from a dict that validate_entry() has already accepted."""
        return Entry(**{name: data[name] for name in ENTRY_FIELDS})


def signed_bytes(entry: dict[str, Any]) -> bytes:
    """The exact bytes that are signed and hashed: prefix + canonical JSON without sig."""
    unsigned = {name: value for name, value in entry.items() if name != "sig"}
    return config.ENTRY_SIGN_PREFIX + canonical.dumps(unsigned)


def entry_hash(entry: dict[str, Any]) -> str:
    """BLAKE2b-256 of the signed bytes, as 64 hex chars."""
    return hashlib.blake2b(signed_bytes(entry), digest_size=32).hexdigest()


def create_entry(
    identity: Identity,
    group_id: str,
    head: Head | None,
    kind: str,
    body: dict[str, Any],
    ts: int,
) -> Entry:
    """Make and sign the next entry in `identity`'s log, continuing from `head`."""
    fields: dict[str, Any] = {
        "v": config.ENTRY_VERSION,
        "group": group_id,
        "author": identity.pubkey,
        "seq": 1 if head is None else head.seq + 1,
        "prev": None if head is None else head.hash,
        "ts": ts,
        "kind": kind,
        "body": body,
    }
    fields["sig"] = identity.sign(signed_bytes(fields))
    return Entry.from_dict(fields)


def parse_entry(raw: bytes) -> dict[str, Any] | Reason:
    """Strictly parse raw entry bytes. Returns the dict, or a Reason if it can't be read."""
    if len(raw) > config.MAX_ENTRY_BYTES:
        return Reason("too_large", f"entry is {len(raw)} bytes; the limit is {config.MAX_ENTRY_BYTES}")
    try:
        return canonical.loads(raw)
    except canonical.CanonicalError as error:
        return Reason("bad_json", f"entry is not strict JSON: {error}")


def validate_entry(
    entry: Any,
    head: Head | None,
    members: set[str],
    admin: str | None,
    group_id: str,
) -> Reason | None:
    """Check every rule for appending `entry` after `head`. Returns None if it's valid.

    `entry` is a parsed dict straight from disk or the network: trust nothing.
    This is validate_contents() (is it a genuine entry?) followed by
    check_chain() (does it fit right after what we hold?).
    """
    reason = validate_contents(entry, members, admin, group_id)
    if reason is not None:
        return reason
    return check_chain(entry, head)


def validate_contents(entry: Any, members: set[str], admin: str | None, group_id: str) -> Reason | None:
    """Every rule except the chain position: shape, size, group, signature, body, authority.

    The store uses this on its own to tell a fork (a genuine, validly signed
    entry in the wrong place) from garbage, which must never mark anyone forked.
    The checks run cheapest-first, and each returns the first problem it finds.
    """
    reason = _check_shape(entry)
    if reason is not None:
        return reason
    try:
        raw_size = len(canonical.dumps(entry))
    except canonical.CanonicalError as error:
        # Only possible for dicts built in code, since parse_entry() is strict.
        return Reason("bad_json", f"entry can't be canonically encoded: {error}")
    if raw_size > config.MAX_ENTRY_BYTES:
        return Reason("too_large", f"entry is {raw_size} bytes; the limit is {config.MAX_ENTRY_BYTES}")
    label = _label(entry)
    if entry["v"] != config.ENTRY_VERSION:
        return Reason("bad_version", f"{label} has unknown format version {entry['v']}")
    if entry["group"] != group_id:
        return Reason("wrong_group", f"{label} belongs to a different group. Wrong party, friend.")
    if entry["kind"] not in config.ENTRY_KINDS:
        return Reason("unknown_kind", f"{label} has unknown kind {entry['kind']!r}")
    if not verify_signature(entry["author"], signed_bytes(entry), entry["sig"]):
        return Reason("bad_signature", f"{label} has a forged or broken signature. Nice try.")
    reason = _check_body(entry, label)
    if reason is not None:
        return reason
    return _check_authority(entry, label, members, admin)


def check_chain(entry: dict[str, Any], head: Head | None) -> Reason | None:
    """The entry must continue the author's chain exactly where we are.

    Only call this on an entry whose shape validate_contents() has accepted.
    """
    label = _label(entry)
    if head is None:
        if entry["seq"] != 1:
            return Reason("bad_seq", f"{label}: we have nothing from this author, so expected #1")
        if entry["prev"] is not None:
            return Reason("bad_prev", f"{label} is #1 but has a prev hash")
        return None
    if entry["seq"] != head.seq + 1:
        return Reason("bad_seq", f"{label}: expected #{head.seq + 1} after #{head.seq}")
    if entry["prev"] != head.hash:
        return Reason("bad_prev", f"{label} doesn't link to the entry we hold as #{head.seq}")
    return None


def now_ms() -> int:
    """The current time in Unix milliseconds (time_ns avoids float rounding)."""
    return time.time_ns() // 1_000_000


def _label(entry: dict[str, Any]) -> str:
    return f"entry #{entry['seq']} by {entry['author'][:8]}"


def _check_shape(entry: Any) -> Reason | None:
    """Exact field set and the type/format of every field."""
    if not isinstance(entry, dict):
        return Reason("bad_shape", "entry is not a JSON object")
    if set(entry) != ENTRY_FIELDS:
        missing = sorted(ENTRY_FIELDS - set(entry))
        extra = sorted(set(entry) - ENTRY_FIELDS)
        return Reason("bad_shape", f"entry fields are wrong (missing {missing}, unexpected {extra})")
    problems = {
        "v": canonical.is_int(entry["v"]),
        "group": is_hex64(entry["group"]),
        "author": is_hex64(entry["author"]),
        "seq": canonical.is_int(entry["seq"]) and 1 <= entry["seq"] <= config.MAX_SEQ,
        "prev": entry["prev"] is None or is_hex64(entry["prev"]),
        "ts": canonical.is_int(entry["ts"]) and 0 <= entry["ts"] <= config.MAX_TS,
        "kind": isinstance(entry["kind"], str),
        "body": isinstance(entry["body"], dict),
        "sig": isinstance(entry["sig"], str) and HEX128.fullmatch(entry["sig"]) is not None,
    }
    bad = [name for name, ok in problems.items() if not ok]
    if bad:
        return Reason("bad_shape", f"entry has invalid field(s): {', '.join(bad)}")
    return None


def _check_body(entry: dict[str, Any], label: str) -> Reason | None:
    """Kind-specific body schema."""
    body = entry["body"]
    kind = entry["kind"]
    if kind == "group_create":
        ok = set(body) == {"admin"} and is_hex64(body["admin"])
    elif kind == "member_add":
        ok = set(body) == {"member"} and is_hex64(body["member"])
    else:
        # profile and post: only an encrypted box. What's inside is checked by
        # whoever holds the group key (feed.py), since nobody else can see it.
        ok = set(body) == {"box"} and _is_box(body["box"])
    if not ok:
        return Reason("bad_body", f"{label} has an invalid {kind} body")
    return None


def _check_authority(entry: dict[str, Any], label: str, members: set[str], admin: str | None) -> Reason | None:
    """Who may write what: only the admin creates the group and adds members."""
    author = entry["author"]
    if entry["kind"] == "group_create":
        # The one entry a non-member may write: the admin's very first one.
        if admin is None or author != admin or entry["body"]["admin"] != author:
            return Reason("not_admin", f"{label} tries to create the group but isn't its admin")
        if entry["seq"] != 1:
            return Reason("bad_seq", f"{label}: group_create must be the admin's entry #1")
        return None
    if author not in members:
        return Reason("not_member", f"{label} is from someone who isn't a member of this group")
    if entry["kind"] == "member_add" and author != admin:
        return Reason("not_admin", f"{label} adds a member, but only the admin can do that")
    return None


def is_hex64(value: Any) -> bool:
    """True for exactly 64 lowercase hex characters: pubkeys, hashes, group and chunk IDs."""
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _is_box(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        return False
    return len(decoded) >= MIN_BOX_BYTES

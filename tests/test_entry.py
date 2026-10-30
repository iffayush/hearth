"""Tests for entry.py: signing, hash chains, and every validate_entry() rejection reason."""

import base64
from typing import Any

import pytest

from hearth import canonical, config
from hearth.entry import Entry, Head, Reason, create_entry, entry_hash, parse_entry, signed_bytes, validate_entry
from hearth.identity import Identity

from conftest import fake_box

GROUP = "ab" * 32
OTHER_GROUP = "cd" * 32
TS = 1_761_400_000_000


def sign_post(identity: Identity, head: Head | None = None, text: str = "hello") -> Entry:
    return create_entry(identity, GROUP, head, "post", {"box": fake_box(text)}, TS)


def check(entry: dict[str, Any], head: Head | None, author: Identity, admin: str | None = None) -> str | None:
    """Validate with `author` as the only member; return the reason code or None."""
    reason = validate_entry(entry, head, {author.pubkey}, admin, GROUP)
    return None if reason is None else reason.code


def resign(identity: Identity, fields: dict[str, Any]) -> dict[str, Any]:
    """Re-sign altered fields so a test reaches the check after the signature."""
    fields = dict(fields)
    fields["sig"] = identity.sign(signed_bytes(fields))
    return fields


# --- Happy paths -------------------------------------------------------------


def test_first_entry_is_valid(alice: Identity) -> None:
    first = sign_post(alice)
    assert first.seq == 1 and first.prev is None
    assert check(first.to_dict(), None, alice) is None


def test_chain_of_three_is_valid(alice: Identity) -> None:
    head = None
    for number in range(1, 4):
        current = sign_post(alice, head, text=f"post {number}")
        assert check(current.to_dict(), head, alice) is None
        head = Head(current.seq, current.compute_hash())
    assert head.seq == 3


def test_signed_bytes_start_with_domain_prefix(alice: Identity) -> None:
    data = signed_bytes(sign_post(alice).to_dict())
    assert data.startswith(b"hearth/entry/v1\n")
    assert b'"sig"' not in data


def test_hash_ignores_signature_field(alice: Identity) -> None:
    fields = sign_post(alice).to_dict()
    assert entry_hash(fields) == entry_hash({**fields, "sig": "00" * 64})


def test_raw_round_trips_through_strict_parser(alice: Identity) -> None:
    first = sign_post(alice, text="chai ☕ and samosas")
    parsed = parse_entry(first.raw())
    assert parsed == first.to_dict()
    assert Entry.from_dict(parsed) == first


# --- Tampering ---------------------------------------------------------------


def test_changing_any_byte_fails_verification(alice: Identity) -> None:
    raw = sign_post(alice).raw()
    for position in range(len(raw)):
        tampered = bytearray(raw)
        # Swap in a different printable character at this position.
        tampered[position] = ord("x") if raw[position] != ord("x") else ord("y")
        parsed = parse_entry(bytes(tampered))
        if isinstance(parsed, dict):
            assert check(parsed, None, alice) is not None, f"byte {position} change went unnoticed"


def test_changed_text_is_a_bad_signature(alice: Identity) -> None:
    fields = sign_post(alice).to_dict()
    fields["body"] = {"box": fake_box("hellO")}
    assert check(fields, None, alice) == "bad_signature"


def test_bob_cannot_pretend_to_be_alice(alice: Identity, bob: Identity) -> None:
    forged = resign(bob, {**sign_post(alice).to_dict(), "author": alice.pubkey})
    assert check(forged, None, alice) == "bad_signature"


# --- The head rules ----------------------------------------------------------


def test_first_entry_must_be_seq_1(alice: Identity) -> None:
    second = sign_post(alice, Head(1, "11" * 32))
    assert check(second.to_dict(), None, alice) == "bad_seq"


def test_seq_1_with_a_prev_is_rejected(alice: Identity) -> None:
    fields = resign(alice, {**sign_post(alice).to_dict(), "prev": "11" * 32})
    assert check(fields, None, alice) == "bad_prev"


def test_wrong_seq_against_head_is_rejected(alice: Identity) -> None:
    first = sign_post(alice)
    head = Head(1, first.compute_hash())
    skipping = sign_post(alice, Head(2, head.hash))  # claims to be #3
    assert check(skipping.to_dict(), head, alice) == "bad_seq"


def test_replaying_an_old_entry_is_rejected(alice: Identity) -> None:
    first = sign_post(alice)
    second = sign_post(alice, Head(1, first.compute_hash()))
    head = Head(2, second.compute_hash())
    assert check(first.to_dict(), head, alice) == "bad_seq"


def test_broken_prev_is_rejected(alice: Identity) -> None:
    first = sign_post(alice)
    head = Head(1, first.compute_hash())
    wrong_link = sign_post(alice, Head(1, "22" * 32))
    assert check(wrong_link.to_dict(), head, alice) == "bad_prev"


# --- Group, kind, body, membership --------------------------------------------


def test_wrong_group_is_rejected(alice: Identity) -> None:
    elsewhere = create_entry(alice, OTHER_GROUP, None, "post", {"box": fake_box("hi")}, TS)
    assert check(elsewhere.to_dict(), None, alice) == "wrong_group"


def test_unknown_kind_is_rejected(alice: Identity) -> None:
    odd = create_entry(alice, GROUP, None, "poll", {"box": fake_box("hi")}, TS)
    assert check(odd.to_dict(), None, alice) == "unknown_kind"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"box": 5},
        {"box": "not base64!"},
        {"box": "c2hvcnQ="},  # valid base64, but too short to be a SecretBox
        {"text": "plaintext posts are gone since Phase 5"},
        {"box": fake_box("hi"), "extra": 1},
    ],
)
def test_bad_post_body_is_rejected(alice: Identity, body: dict[str, Any]) -> None:
    entry = create_entry(alice, GROUP, None, "post", body, TS)
    assert check(entry.to_dict(), None, alice) == "bad_body"


def test_non_member_is_rejected(alice: Identity, bob: Identity) -> None:
    assert check(sign_post(bob).to_dict(), None, alice) == "not_member"


def test_admin_can_create_group_without_being_a_member(alice: Identity) -> None:
    create = create_entry(alice, GROUP, None, "group_create", {"admin": alice.pubkey}, TS)
    reason = validate_entry(create.to_dict(), None, set(), alice.pubkey, GROUP)
    assert reason is None


def test_non_admin_cannot_create_group(alice: Identity, bob: Identity) -> None:
    create = create_entry(bob, GROUP, None, "group_create", {"admin": bob.pubkey}, TS)
    reason = validate_entry(create.to_dict(), None, set(), alice.pubkey, GROUP)
    assert reason is not None and reason.code == "not_admin"


def test_only_admin_adds_members(alice: Identity, bob: Identity) -> None:
    add = create_entry(bob, GROUP, None, "member_add", {"member": bob.pubkey}, TS)
    reason = validate_entry(add.to_dict(), None, {alice.pubkey, bob.pubkey}, alice.pubkey, GROUP)
    assert reason is not None and reason.code == "not_admin"


def test_profile_body_needs_a_real_box(alice: Identity) -> None:
    good_box = base64.b64encode(b"\0" * 60).decode()
    good = create_entry(alice, GROUP, None, "profile", {"box": good_box}, TS)
    bad = create_entry(alice, GROUP, None, "profile", {"box": "not base64!"}, TS)
    assert check(good.to_dict(), None, alice) is None
    assert check(bad.to_dict(), None, alice) == "bad_body"


# --- Shape and size ------------------------------------------------------------


def test_true_as_seq_is_rejected(alice: Identity) -> None:
    fields = resign(alice, {**sign_post(alice).to_dict(), "seq": True})
    assert check(fields, None, alice) == "bad_shape"


def test_missing_and_extra_fields_are_rejected(alice: Identity) -> None:
    fields = sign_post(alice).to_dict()
    del fields["ts"]
    assert check(fields, None, alice) == "bad_shape"
    assert check({**sign_post(alice).to_dict(), "mood": "sunny"}, None, alice) == "bad_shape"


def test_uppercase_hex_is_rejected(alice: Identity) -> None:
    fields = {**sign_post(alice).to_dict(), "author": alice.pubkey.upper()}
    assert check(fields, None, alice) == "bad_shape"


def test_not_a_dict_is_rejected(alice: Identity) -> None:
    assert check(["not", "an", "entry"], None, alice) == "bad_shape"  # type: ignore[arg-type]


def test_oversized_entry_is_rejected(alice: Identity) -> None:
    # Size is checked before the body schema, so an over-long text is enough.
    big = create_entry(alice, GROUP, None, "post", {"box": fake_box("x" * 70_000)}, TS)
    assert check(big.to_dict(), None, alice) == "too_large"
    reason = parse_entry(big.raw())
    assert isinstance(reason, Reason) and reason.code == "too_large"


def test_parse_rejects_floats_and_duplicate_keys() -> None:
    for raw in (b'{"seq":1.0}', b'{"seq":1,"seq":2}'):
        reason = parse_entry(raw)
        assert isinstance(reason, Reason) and reason.code == "bad_json"


def test_raw_is_canonical(alice: Identity) -> None:
    first = sign_post(alice)
    assert first.raw() == canonical.dumps(first.to_dict())

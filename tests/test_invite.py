"""Tests for invite.py: links, single use, expiry, the all-or-nothing redemption,
and the joiner's checks on INVITE_OK."""

import base64
from pathlib import Path

import pytest
from nacl.public import SealedBox

from hearth import actions, crypto, invite, protocol
from hearth.identity import Identity
from hearth.invite import InviteError, InviteLink
from hearth.store import Store
from hearth.tables import InviteTable

NOW = 1_761_400_000_000
HOUR = 3600


class Home:
    """An admin's store with a group, ready to make and redeem invites."""

    def __init__(self, tmp_path: Path) -> None:
        self.admin = Identity.generate()
        self.store = Store.open(tmp_path / "home.db")
        self.group_key = crypto.new_group_key()
        self.group = actions.create_group(self.store, self.admin)["group"]

    def invite(self, expires_in: int = 48 * HOUR) -> InviteLink:
        return invite.create_invite(self.store, self.admin, "home.example.net", 7777, expires_in, NOW)

    def redeem(self, link: InviteLink, joiner: Identity, now: int = NOW + 1000) -> dict[str, object]:
        return invite.redeem(self.store, self.admin, self.group_key, link.token, joiner.pubkey,
                             joiner.encryption_pubkey, now)


@pytest.fixture
def home(tmp_path: Path) -> Home:
    created = Home(tmp_path)
    yield created
    created.store.close()


# --- Links ---------------------------------------------------------------------------


def test_link_round_trip(home: Home) -> None:
    link = home.invite()
    text = link.text()
    assert text.startswith("hearth://join?h=home.example.net:7777&k=")
    assert invite.parse_link(text) == link


@pytest.mark.parametrize(
    "mangle, problem",
    [
        (lambda text: text.replace("hearth://", "https://"), "isn't a Hearth invite link"),
        (lambda text: text.split("&t=")[0], "exactly one each"),
        (lambda text: text + "&t=" + "A" * 43, "exactly one each"),
        (lambda text: text[:-5], "token is malformed"),
        (lambda text: text.replace(":7777", ":99999"), "isn't host:port"),
        (lambda text: text.replace("&k=", "&k=XYZ"), "isn't 64 lowercase hex"),
    ],
)
def test_garbled_links_are_refused(home: Home, mangle, problem: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InviteError, match=problem):
        invite.parse_link(mangle(home.invite().text()))


@pytest.mark.parametrize("text, seconds", [("90s", 90), ("30m", 1800), ("48h", 172800), ("7d", 604800)])
def test_durations(text: str, seconds: int) -> None:
    assert invite.parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["0h", "48", "h", "1w", "-5m", "1.5h"])
def test_bad_durations(text: str) -> None:
    with pytest.raises(InviteError):
        invite.parse_duration(text)


# --- Creating ------------------------------------------------------------------------


def test_only_the_token_hash_is_stored(home: Home, tmp_path: Path) -> None:
    link = home.invite()
    row = home.store.invites.get(invite.token_hash(link.token))
    assert row is not None and row.expires == NOW + 48 * HOUR * 1000 and row.used_by is None
    assert link.token.encode() not in (tmp_path / "home.db").read_bytes()


def test_only_the_admin_creates_invites(home: Home) -> None:
    with pytest.raises(InviteError, match="only the group's admin"):
        invite.create_invite(home.store, Identity.generate(), "x", 1, HOUR, NOW)


# --- Redeeming -----------------------------------------------------------------------


def test_redeem_adds_the_member_and_seals_the_key_to_them(home: Home, bob: Identity) -> None:
    reply = home.redeem(home.invite(), bob)
    assert reply["t"] == "INVITE_OK" and reply["group"] == home.group and reply["admin"] == home.admin.pubkey
    assert bob.pubkey in home.store.members()
    unsealed = SealedBox(bob.encryption_key).decrypt(base64.b64decode(reply["sealed_key"]))  # type: ignore[arg-type]
    assert unsealed == home.group_key
    assert protocol.parse_message(protocol.encode_message(reply)) == reply  # it's a valid wire message


def test_second_use_by_another_pubkey_fails(home: Home, bob: Identity) -> None:
    link = home.invite()
    home.redeem(link, bob)
    with pytest.raises(InviteError, match="already been used"):
        home.redeem(link, Identity.generate())


def test_second_use_by_the_same_pubkey_also_fails(home: Home, bob: Identity) -> None:
    # The owner chose single use, full stop: a lost INVITE_OK means a new invite.
    link = home.invite()
    home.redeem(link, bob)
    with pytest.raises(InviteError, match="already been used"):
        home.redeem(link, bob)
    assert [entry.kind for entry in home.store.get_range(home.admin.pubkey, 1, 9)] == ["group_create", "member_add"]


def test_expired_link_fails(home: Home, bob: Identity) -> None:
    link = home.invite(expires_in=HOUR)
    with pytest.raises(InviteError, match="expired"):
        home.redeem(link, bob, now=NOW + HOUR * 1000 + 1)
    assert bob.pubkey not in home.store.members()


def test_unknown_token_fails(home: Home, bob: Identity) -> None:
    fake = InviteLink("x", 1, home.admin.pubkey, home.group, "A" * 43)
    with pytest.raises(InviteError, match="unknown invite"):
        home.redeem(fake, bob)


def test_a_crash_mid_redemption_leaves_the_token_usable(
    home: Home, bob: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = home.invite()

    def power_cut(*args: object) -> None:
        raise RuntimeError("the lights went out between member_add and saving the sealed key")

    # The transaction marks the token, appends member_add, then saves the sealed key. Fail at the end.
    monkeypatch.setattr(InviteTable, "save_sealed_key", power_cut)
    with pytest.raises(RuntimeError):
        home.redeem(link, bob)
    monkeypatch.undo()

    # Everything rolled back: no member_add, token unused...
    assert bob.pubkey not in home.store.members()
    assert home.store.head(home.admin.pubkey).seq == 1  # type: ignore[union-attr]
    assert home.store.invites.get(invite.token_hash(link.token)).used_by is None  # type: ignore[union-attr]
    # ...so the very same link still works.
    home.redeem(link, bob)
    assert bob.pubkey in home.store.members()


# --- The joiner's checks --------------------------------------------------------------


def make_ok(home: Home, joiner: Identity, **changes: str) -> dict[str, object]:
    sealed = base64.b64encode(bytes(SealedBox(joiner.encryption_key.public_key).encrypt(home.group_key))).decode()
    return {**protocol.make_invite_ok(home.group, home.admin.pubkey, sealed), **changes}


def test_good_invite_ok_unseals(home: Home, bob: Identity) -> None:
    link = home.invite()
    assert invite.open_invite_ok(make_ok(home, bob), link, bob) == home.group_key


@pytest.mark.parametrize(
    "changes, problem",
    [
        ({"group": "ee" * 32}, "different group"),
        ({"admin": "ee" * 32}, "different admin"),
    ],
)
def test_invite_ok_for_the_wrong_group_or_admin_is_rejected(
    home: Home, bob: Identity, changes: dict[str, str], problem: str
) -> None:
    with pytest.raises(InviteError, match=problem):
        invite.open_invite_ok(make_ok(home, bob, **changes), home.invite(), bob)


def test_invite_ok_with_a_key_that_wont_unseal_is_rejected(home: Home, bob: Identity) -> None:
    sealed_for_someone_else = make_ok(home, Identity.generate())
    with pytest.raises(InviteError, match="won't unseal"):
        invite.open_invite_ok(sealed_for_someone_else, home.invite(), bob)


def test_invite_ok_sealing_the_wrong_size_key_is_rejected(home: Home, bob: Identity) -> None:
    short = base64.b64encode(bytes(SealedBox(bob.encryption_key.public_key).encrypt(b"too short"))).decode()
    with pytest.raises(InviteError, match="wrong size"):
        invite.open_invite_ok({**make_ok(home, bob), "sealed_key": short}, home.invite(), bob)


def test_unusable_joiner_encryption_key_is_refused(home: Home, bob: Identity) -> None:
    with pytest.raises(InviteError):
        invite.redeem(home.store, home.admin, home.group_key, home.invite().token, bob.pubkey, "zz" * 32, NOW)


def test_sealed_key_is_kept_with_the_invite(home: Home, bob: Identity) -> None:
    link = home.invite()
    reply = home.redeem(link, bob)
    row = home.store.invites.get(invite.token_hash(link.token))
    assert row is not None and row.used_by == bob.pubkey and row.sealed_key == reply["sealed_key"]

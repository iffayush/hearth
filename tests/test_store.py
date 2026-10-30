"""Tests for store.py: appending, duplicates, gaps, forks, membership and sort_ts."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from hearth import audit, canonical
from hearth.entry import Entry, Head, create_entry
from hearth.identity import Identity
from hearth.store import ADDED, DUPLICATE, FORKED, REJECTED, SCHEMA_VERSION, Store, StoreError

from conftest import fake_box

GROUP = "ab" * 32
NOW = 1_761_400_000_000


@dataclass
class FakeClock:
    """A clock the test moves by hand, so sort_ts is predictable."""

    now: int = NOW

    def __call__(self) -> int:
        return self.now


def open_store(tmp_path: Path, clock: FakeClock | None = None) -> Store:
    return Store.open(tmp_path / "hearth.db", clock or FakeClock())


def start_group(store: Store, admin: Identity) -> Entry:
    created = create_entry(admin, GROUP, None, "group_create", {"admin": admin.pubkey}, NOW)
    assert store.create_group(created.raw()).status == ADDED
    return created


def post(store: Store, author: Identity, text: str, ts: int = NOW) -> Entry:
    """Sign the author's next post (continuing from the store's head)."""
    return create_entry(author, GROUP, store.head(author.pubkey), "post", {"box": fake_box(text)}, ts)


def add_member(store: Store, admin: Identity, member: Identity) -> None:
    added = create_entry(admin, GROUP, store.head(admin.pubkey), "member_add", {"member": member.pubkey}, NOW)
    assert store.append_entry(added.raw()).status == ADDED


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = open_store(tmp_path)
    yield opened
    opened.close()


# --- Schema and group setup ----------------------------------------------------


def test_new_database_has_schema_version_and_wal(store: Store, tmp_path: Path) -> None:
    raw_db = sqlite3.connect(tmp_path / "hearth.db")
    assert raw_db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == (str(SCHEMA_VERSION),)
    assert raw_db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    raw_db.close()


def test_wrong_schema_version_is_refused(tmp_path: Path) -> None:
    open_store(tmp_path).close()
    raw_db = sqlite3.connect(tmp_path / "hearth.db")
    with raw_db:
        raw_db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    raw_db.close()
    with pytest.raises(StoreError, match="delete the data directory"):
        open_store(tmp_path)


def test_create_group_sets_meta_and_makes_admin_a_member(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    assert store.group_id() == GROUP
    assert store.admin() == alice.pubkey
    assert store.members() == {alice.pubkey}


def test_create_group_twice_is_refused(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    with pytest.raises(StoreError, match="already belongs"):
        start_group(store, alice)


def test_group_create_with_someone_elses_admin_is_rejected(store: Store, alice: Identity, bob: Identity) -> None:
    # Bob signs a group_create naming Alice as admin: author != admin.
    sneaky = create_entry(bob, GROUP, None, "group_create", {"admin": alice.pubkey}, NOW)
    result = store.create_group(sneaky.raw())
    assert result.status == REJECTED and result.reason.code == "not_admin"
    assert store.group_id() is None


def test_create_group_needs_a_group_create_entry(store: Store, alice: Identity) -> None:
    result = store.create_group(create_entry(alice, GROUP, None, "post", {"box": fake_box("hi")}, NOW).raw())
    assert result.status == REJECTED and result.reason.code == "bad_kind"


def test_append_before_any_group_is_rejected(store: Store, alice: Identity) -> None:
    result = store.append_entry(create_entry(alice, GROUP, None, "post", {"box": fake_box("hi")}, NOW).raw())
    assert result.status == REJECTED and result.reason.code == "no_group"


# --- Appending and reading ------------------------------------------------------


def test_posts_append_and_heads_advance(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    first = post(store, alice, "one")
    assert store.append_entry(first.raw()).status == ADDED
    second = post(store, alice, "two")
    assert store.append_entry(second.raw()).status == ADDED
    assert store.head(alice.pubkey) == Head(3, second.compute_hash())
    assert store.heads() == {alice.pubkey: Head(3, second.compute_hash())}
    assert [row.seq for row in store.get_range(alice.pubkey, 2, 3)] == [2, 3]
    assert store.get_entry(alice.pubkey, 2).hash == first.compute_hash()
    assert store.get_entry(alice.pubkey, 99) is None


def test_entries_survive_a_restart(tmp_path: Path, alice: Identity) -> None:
    store = open_store(tmp_path)
    start_group(store, alice)
    written = post(store, alice, "still here after a nap")
    store.append_entry(written.raw())
    store.close()

    reopened = open_store(tmp_path)
    assert reopened.group_id() == GROUP
    assert reopened.members() == {alice.pubkey}
    assert reopened.get_entry(alice.pubkey, 2).raw == written.raw().decode("utf-8")
    reopened.close()


def test_identical_duplicate_is_a_silent_no_op(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    first = post(store, alice, "say it once")
    assert store.append_entry(first.raw()).status == ADDED
    assert store.append_entry(first.raw()).status == DUPLICATE
    assert store.head(alice.pubkey).seq == 2
    assert store.forked_authors() == []


def test_raw_is_the_canonical_re_encoding(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    signed = post(store, alice, "spaces everywhere")
    # Same entry, but with spaces and reordered keys: valid JSON, not canonical.
    sloppy = b'{ "v": 1, ' + signed.raw()[1:].replace(b',"v":1', b"")
    assert store.append_entry(sloppy).status == ADDED
    assert store.get_entry(alice.pubkey, 2).raw == signed.raw().decode("utf-8")


# --- Rejections: gaps, wrong group, non-members ------------------------------------


def test_gap_is_rejected_without_marking_a_fork(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    skipping = create_entry(alice, GROUP, Head(2, "11" * 32), "post", {"box": fake_box("#3 already?")}, NOW)
    result = store.append_entry(skipping.raw())
    assert result.status == REJECTED and result.reason.code == "bad_seq"
    assert store.forked_authors() == []


def test_new_author_must_start_at_seq_1(store: Store, alice: Identity, bob: Identity) -> None:
    start_group(store, alice)
    add_member(store, alice, bob)
    late_start = create_entry(bob, GROUP, Head(1, "22" * 32), "post", {"box": fake_box("hi")}, NOW)
    assert store.append_entry(late_start.raw()).reason.code == "bad_seq"


def test_wrong_group_id_is_rejected(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    elsewhere = create_entry(alice, "cd" * 32, store.head(alice.pubkey), "post", {"box": fake_box("hi")}, NOW)
    result = store.append_entry(elsewhere.raw())
    assert result.status == REJECTED and result.reason.code == "wrong_group"


def test_non_member_post_is_rejected_until_added(store: Store, alice: Identity, bob: Identity) -> None:
    start_group(store, alice)
    bobs_post = post(store, bob, "let me in")
    assert store.append_entry(bobs_post.raw()).reason.code == "not_member"
    add_member(store, alice, bob)
    assert store.members() == {alice.pubkey, bob.pubkey}
    assert store.append_entry(bobs_post.raw()).status == ADDED


def test_add_member_twice_keeps_the_first_seq(store: Store, alice: Identity) -> None:
    store.add_member(alice.pubkey, 2)
    store.add_member(alice.pubkey, 7)
    assert store._db.execute("SELECT added_seq FROM members").fetchall() == [(2,)]


# --- Forks ------------------------------------------------------------------------


def test_conflicting_entry_marks_author_forked(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    head = store.head(alice.pubkey)
    original = create_entry(alice, GROUP, head, "post", {"box": fake_box("from laptop A")}, NOW)
    rival = create_entry(alice, GROUP, head, "post", {"box": fake_box("from laptop B")}, NOW)
    assert store.append_entry(original.raw()).status == ADDED
    result = store.append_entry(rival.raw())
    assert result.status == FORKED and result.reason.code == "fork"
    assert store.forked_authors() == [alice.pubkey]
    # The entry we held first stays; the rival is never stored.
    assert store.get_entry(alice.pubkey, 2).hash == original.compute_hash()


def test_next_entry_with_unknown_prev_marks_fork(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    other_history = create_entry(alice, GROUP, Head(1, "33" * 32), "post", {"box": fake_box("parallel universe")}, NOW)
    result = store.append_entry(other_history.raw())
    assert result.status == FORKED
    assert store.forked_authors() == [alice.pubkey]


def test_forged_conflict_cannot_frame_alice_as_forked(store: Store, alice: Identity, bob: Identity) -> None:
    start_group(store, alice)
    add_member(store, alice, bob)
    # Bob builds an "alice #2" signed with his own key. Bad signature: no fork flag.
    forged = create_entry(bob, GROUP, Head(1, "44" * 32), "post", {"box": fake_box("alice said this")}, NOW).to_dict()
    forged["author"] = alice.pubkey
    result = store.append_entry(canonical.dumps(forged))
    assert result.status == REJECTED and result.reason.code == "bad_signature"
    assert store.forked_authors() == []


def test_fork_flag_is_listed_once_and_survives_restart(tmp_path: Path, alice: Identity) -> None:
    store = open_store(tmp_path)
    start_group(store, alice)
    head = store.head(alice.pubkey)
    store.append_entry(create_entry(alice, GROUP, head, "post", {"box": fake_box("a")}, NOW).raw())
    for text in ("b", "c"):
        store.append_entry(create_entry(alice, GROUP, head, "post", {"box": fake_box(text)}, NOW).raw())
    store.close()
    reopened = open_store(tmp_path)
    assert reopened.forked_authors() == [alice.pubkey]
    reopened.close()


# --- sort_ts ------------------------------------------------------------------------


def test_future_dated_entry_sorts_at_arrival_time(tmp_path: Path, alice: Identity) -> None:
    clock = FakeClock(NOW)
    store = open_store(tmp_path, clock)
    start_group(store, alice)
    from_the_future = post(store, alice, "greetings from 2099", ts=NOW + 10**12)
    store.append_entry(from_the_future.raw())
    stored = store.get_entry(alice.pubkey, 2)
    assert stored.ts == NOW + 10**12
    assert stored.sort_ts == NOW
    store.close()


def test_past_dated_entry_keeps_its_own_ts(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    store.append_entry(post(store, alice, "throwback", ts=NOW - 5000).raw())
    assert store.get_entry(alice.pubkey, 2).sort_ts == NOW - 5000


def test_sort_ts_never_changes_once_stored(tmp_path: Path, alice: Identity) -> None:
    clock = FakeClock(NOW)
    store = open_store(tmp_path, clock)
    start_group(store, alice)
    future = post(store, alice, "time traveller", ts=NOW + 60_000)
    store.append_entry(future.raw())
    clock.now = NOW + 120_000  # later, so min(ts, now) would now give ts
    assert store.append_entry(future.raw()).status == DUPLICATE
    assert store.get_entry(alice.pubkey, 2).sort_ts == NOW
    store.close()
    reopened = open_store(tmp_path, clock)
    assert reopened.get_entry(alice.pubkey, 2).sort_ts == NOW
    reopened.close()


# --- Re-verification ---------------------------------------------------------------


def problems_in(store: Store) -> list[tuple[int, str]]:
    return [(problem["seq"], problem["code"]) for problem in audit.verify(store)["problems"]]


def test_verify_is_clean_for_honest_data(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    store.append_entry(post(store, alice, "honest").raw())
    assert problems_in(store) == []


def test_verify_catches_a_tampered_row(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    store.append_entry(post(store, alice, "the cake is real", ts=NOW + 1).raw())
    with store._db:
        # Change one digit of the signed timestamp: the signature no longer matches.
        store._db.execute(f"UPDATE entries SET raw = replace(raw, '{NOW + 1}', '{NOW + 2}') WHERE seq = 2")
    assert problems_in(store) == [(2, "bad_signature")]


def test_verify_catches_a_swapped_hash(store: Store, alice: Identity) -> None:
    start_group(store, alice)
    store.append_entry(post(store, alice, "hash me").raw())
    with store._db:
        store._db.execute("UPDATE entries SET hash = ? WHERE seq = 2", ("55" * 32,))
    assert problems_in(store) == [(2, "hash_mismatch")]


def test_chunk_and_outbox_tables(store: Store) -> None:
    store.chunk_table.add("aa" * 32, 100)
    store.chunk_table.add("aa" * 32, 100)  # the same chunk twice counts once
    store.chunk_table.add("bb" * 32, 50)
    assert store.chunk_table.usage() == 150
    row_id = store.outbox_table.add(NOW, fake_box("waiting"), '["cc"]')
    store.outbox_table.set_acked(row_id, '["cc"]')
    assert store.outbox_table.rows() == [(row_id, NOW, fake_box("waiting"), '["cc"]', '["cc"]')]
    store.outbox_table.delete(row_id)
    assert store.outbox_table.rows() == []

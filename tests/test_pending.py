"""Tests for pending.py: the bounded waiting room for not-yet-member entries."""

from hearth.entry import Head, create_entry
from hearth.identity import Identity
from hearth.pending import PendingQueue

from conftest import fake_box

GROUP = "ab" * 32


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def raw_post(author: Identity, seq: int = 1, text: str = "hi") -> str:
    # seq > 1 needs a prev; any hash will do, the queue doesn't check chains.
    head = None if seq == 1 else Head(seq - 1, "11" * 32)
    return create_entry(author, GROUP, head, "post", {"box": fake_box(text)}, 1).raw().decode()


def make_queue(clock: Clock, max_entries: int = 10, max_bytes: int = 1_000_000, max_age: float = 600) -> PendingQueue:
    return PendingQueue(max_entries, max_bytes, max_age, clock)


def test_add_and_take_in_author_seq_order(alice: Identity) -> None:
    queue = make_queue(Clock())
    for seq in (3, 1, 2):
        assert queue.add(raw_post(alice, seq), source="peer")
    assert [item.seq for item in queue.take_all()] == [1, 2, 3]
    assert len(queue) == 0


def test_identical_entry_is_held_once(alice: Identity) -> None:
    queue = make_queue(Clock())
    raw = raw_post(alice)
    assert queue.add(raw, source="peer") and queue.add(raw, source="another peer")
    assert len(queue) == 1


def test_entry_count_cap(alice: Identity) -> None:
    queue = make_queue(Clock(), max_entries=2)
    assert queue.add(raw_post(alice, 1), "peer") and queue.add(raw_post(alice, 2), "peer")
    assert not queue.add(raw_post(alice, 3), "peer")


def test_byte_cap_counts_utf8_bytes(alice: Identity) -> None:
    emoji_post = raw_post(alice, 1, "😀" * 1000)  # 1,000 characters, 4,000 bytes
    queue = make_queue(Clock(), max_bytes=len(emoji_post.encode()) + 10)
    assert queue.add(emoji_post, "peer")
    assert not queue.add(raw_post(alice, 2, "😀" * 1000), "peer")
    assert queue.total_bytes() == len(emoji_post.encode())


def test_old_entries_are_evicted(alice: Identity, bob: Identity) -> None:
    clock = Clock()
    queue = make_queue(clock, max_age=600)
    queue.add(raw_post(alice), "peer-a")
    clock.now = 500
    queue.add(raw_post(bob), "peer-b")
    clock.now = 601
    evicted = queue.evict_expired()
    assert [(item.author, item.source) for item in evicted] == [(alice.pubkey, "peer-a")]
    assert queue.holds_author(bob.pubkey) and not queue.holds_author(alice.pubkey)


def test_put_back_keeps_the_original_age(alice: Identity) -> None:
    clock = Clock()
    queue = make_queue(clock, max_age=600)
    queue.add(raw_post(alice), "peer")
    clock.now = 400
    for item in queue.take_all():
        queue.put_back(item)
    clock.now = 601
    assert len(queue.evict_expired()) == 1

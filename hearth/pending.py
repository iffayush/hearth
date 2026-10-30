"""The pending queue: genuine entries whose author isn't a member *yet*.

Membership comes from the admin's log, and entries from different authors can
arrive in any order. If bob's post turns up before the admin's `member_add`
for bob, the store says "not a member". Rather than throw the post away and
fetch it again, sync parks it here and retries after the next membership change.

It's bounded three ways (config.py): 1,000 entries, 4 MiB, 10 minutes. When an
entry expires, sync stops asking the peer that sent it for that author, so a
peer can't keep us busy with posts from someone who never gets added.

Joke: the waiting room. Comfy chairs, old magazines, and a strict ten-minute policy.
"""

from collections.abc import Callable
from dataclasses import dataclass

from hearth.entry import entry_hash, parse_entry


@dataclass
class PendingEntry:
    """One parked entry. `source` is the sync state of the connection it came from."""

    raw: str
    author: str
    seq: int
    hash: str
    # UTF-8 bytes, which is what the 4 MiB cap counts (an emoji is one character but four bytes).
    size: int
    added_at: float
    source: object


class PendingQueue:
    """Entries waiting for their author's member_add. Bounded in count, bytes and age."""

    def __init__(self, max_entries: int, max_bytes: int, max_age: float, clock: Callable[[], float]) -> None:
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._max_age = max_age
        # Injected so tests can age entries without waiting ten minutes.
        self._clock = clock
        self._items: dict[tuple[str, int, str], PendingEntry] = {}

    def __len__(self) -> int:
        return len(self._items)

    def total_bytes(self) -> int:
        return sum(item.size for item in self._items.values())

    def add(self, raw: str, source: object) -> bool:
        """Park `raw`, which the store has already found genuine. Returns False if full."""
        parsed = parse_entry(raw.encode("utf-8"))
        if not isinstance(parsed, dict):
            return False
        key = (parsed["author"], parsed["seq"], entry_hash(parsed))
        if key in self._items:
            return True  # already holding this exact entry: nothing to do
        size = len(raw.encode("utf-8"))
        if len(self._items) >= self._max_entries or self.total_bytes() + size > self._max_bytes:
            return False
        self._items[key] = PendingEntry(raw, key[0], key[1], key[2], size, self._clock(), source)
        return True

    def holds_author(self, author: str) -> bool:
        return any(item.author == author for item in self._items.values())

    def take_all(self) -> list[PendingEntry]:
        """Remove and return everything in (author, seq) order, ready for a retry."""
        items = sorted(self._items.values(), key=lambda item: (item.author, item.seq))
        self._items.clear()
        return items

    def put_back(self, item: PendingEntry) -> None:
        """Return an entry that still can't be applied, keeping its original age."""
        self._items[(item.author, item.seq, item.hash)] = item

    def evict_expired(self) -> list[PendingEntry]:
        """Drop and return entries that have waited longer than max_age."""
        cutoff = self._clock() - self._max_age
        expired = [key for key, item in self._items.items() if item.added_at < cutoff]
        return [self._items.pop(key) for key in expired]

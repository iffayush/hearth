"""Sync: how logs travel between nodes with HEADS, WANT and ENTRIES.

The whole conversation, per connection:

1. On entering ACTIVE, each side sends HEADS: "for every author, here's my
   latest seq and the hash of that entry".
2. For each author where the peer is ahead, we send WANT(author, from, to):
   from = our head + 1, at most 200 entries, one range per author at a time,
   the admin first (membership lives in the admin's log). Where the peer is
   level or behind, we compare hashes at their seq: a different hash means
   two histories exist for that author, a fork.
3. The peer answers each WANT, in order, with ENTRIES (capped at 200 entries
   and 1.5 MiB). We apply them in order and stop at the first bad one. A
   partial answer is fine: we simply ask again from our new head.
4. After storing anything new, we send fresh HEADS to every *other* peer.
   That's how a post reaches a friend who's never online at the same time as
   its author: it waits on the home node, which tells everyone else.
5. A post whose author isn't a member yet waits in the PendingQueue
   (pending.py) and is retried after each membership change.

The home node also serves image chunks here: CHUNK_WANT is answered with a
CHUNK per chunk (or CHUNK_MISSING), and CHUNK_PUSH from a member is checked
(hash, membership, quota), stored, and acknowledged with CHUNK_ACK. The member
side of chunks lives in outbox.py (pushing) and fetcher.py (fetching).

Replies are matched to requests by order: a connection delivers messages in
order and the peer answers each WANT before reading the next, so the first
ENTRIES answers the oldest outstanding WANT. ENTRIES with nothing outstanding
are unsolicited and dropped.

Joke: gossip, but make it cryptographically verified. Aunties everywhere are jealous.
"""

import asyncio
import base64
import binascii
import hmac
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from hearth import canonical, config, protocol
from hearth.chunks import ChunkStore
from hearth.crypto import blake2b_hex
from hearth.entry import Head
from hearth.peer import Peer
from hearth.session import PeerClosed
from hearth.pending import PendingQueue
from hearth.store import ADDED, DUPLICATE, FORKED, Store

logger = logging.getLogger(__name__)


@dataclass
class Want:
    """A WANT we sent that hasn't been answered yet."""

    author: str
    first: int
    last: int


@dataclass
class PeerSync:
    """Sync state for one ACTIVE connection."""

    peer: Peer
    # The peer's latest HEADS, so we can keep requesting after each reply.
    their_heads: dict[str, Head] = field(default_factory=dict)
    heads_received: bool = False
    # author -> the highest seq we've sent this peer (so `join` knows its profile got through).
    served: dict[str, int] = field(default_factory=dict)
    outstanding: deque[Want] = field(default_factory=deque)
    # Authors we stop syncing with this peer: forks, bad entries, expired pending entries.
    blocked: set[str] = field(default_factory=set)
    wants_received: deque[float] = field(default_factory=deque)
    chunk_wants_received: deque[float] = field(default_factory=deque)
    wants_sent: deque[float] = field(default_factory=deque)
    retry_task: asyncio.Task[None] | None = None


class Syncer:
    """Runs sync for every ACTIVE connection of one node. Peer calls the three hooks."""

    def __init__(
        self,
        store: Store,
        chunks: ChunkStore | None = None,
        is_home: bool = False,
        storage_quota: int = config.DEFAULT_STORAGE_QUOTA_GIB * 2**30,
        want_rate_limit: int = config.WANT_RATE_LIMIT,
        chunk_want_rate_limit: int = config.CHUNK_WANT_RATE_LIMIT,
        want_send_limit: int = config.WANT_SEND_LIMIT,
        rate_window: float = config.RATE_WINDOW,
        pending_max_entries: int = config.PENDING_MAX_ENTRIES,
        pending_max_bytes: int = config.PENDING_MAX_BYTES,
        pending_max_age: float = config.PENDING_MAX_AGE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._chunks = chunks
        # Only the home node accepts CHUNK_PUSH, and only up to its storage quota (bytes).
        self._is_home = is_home
        self.storage_quota = storage_quota
        self._want_rate_limit = want_rate_limit
        self._chunk_want_rate_limit = chunk_want_rate_limit
        self._want_send_limit = want_send_limit
        self._rate_window = rate_window
        self._clock = clock
        self.pending = PendingQueue(pending_max_entries, pending_max_bytes, pending_max_age, clock)
        self.peers: dict[Peer, PeerSync] = {}
        # Counted for tests and, later, `status`.
        self.entries_messages_received = 0

    # --- Hooks called by Peer ------------------------------------------------

    async def peer_active(self, peer: Peer) -> None:
        self.peers[peer] = PeerSync(peer)
        await peer.send(protocol.make_heads(self._our_heads()))

    def peer_closed(self, peer: Peer) -> None:
        state = self.peers.pop(peer, None)
        if state is not None and state.retry_task is not None:
            state.retry_task.cancel()

    async def handle(self, peer: Peer, message: dict[str, Any]) -> None:
        state = self.peers.get(peer)
        if state is None:
            return
        kind = message["t"]
        if kind == "HEADS":
            await self._on_heads(state, message["heads"])
        elif kind == "WANT":
            await self._on_want(state, message["author"], message["from"], message["to"])
        elif kind == "ENTRIES":
            await self._on_entries(state, message["entries"])
        elif kind == "CHUNK_WANT":
            await self._on_chunk_want(state, message["ids"])
        elif kind == "CHUNK_PUSH":
            await self._on_chunk_push(state, message["id"], message["data"])
        else:
            logger.info("Dropped %s from %s: nothing handles it yet", kind, peer.label())

    # --- Node-wide actions ---------------------------------------------------

    async def announce(self, exclude: Peer | None = None) -> None:
        """Send our current HEADS to every ACTIVE peer (except `exclude`)."""
        message = protocol.make_heads(self._our_heads())
        for state in list(self.peers.values()):
            if state.peer is exclude:
                continue
            try:
                await state.peer.send(message)
            except (ConnectionError, RuntimeError):
                pass  # that connection is going away; its own task cleans up

    async def resync_all(self) -> None:
        """Re-check every peer's last HEADS against our store and request what's missing."""
        self._evict_expired()
        for state in list(self.peers.values()):
            await self._request_missing(state)

    def caught_up(self, peer: Peer) -> bool:
        """True once we hold everything this peer's last HEADS said it had."""
        state = self.peers.get(peer)
        if state is None or not state.heads_received or state.outstanding:
            return False
        for author, theirs in state.their_heads.items():
            ours = self.store.head(author)
            if author not in state.blocked and (ours is None or ours.seq < theirs.seq):
                return False
        return True

    def has_served(self, peer: Peer, author: str, seq: int) -> bool:
        """True once this peer has fetched `author`'s entries up to `seq` from us."""
        state = self.peers.get(peer)
        return state is not None and state.served.get(author, 0) >= seq

    # --- HEADS ---------------------------------------------------------------

    async def _on_heads(self, state: PeerSync, heads: dict[str, list[Any]]) -> None:
        state.their_heads = {author: Head(seq, hash_) for author, (seq, hash_) in heads.items()}
        state.heads_received = True
        for author, theirs in state.their_heads.items():
            if author not in state.blocked:
                self._check_for_fork(state, author, theirs)
        self._evict_expired()
        await self._request_missing(state)

    def _check_for_fork(self, state: PeerSync, author: str, theirs: Head) -> None:
        """If the peer is level or behind on `author`, its hash must match ours at that seq."""
        stored = self.store.get_entry(author, theirs.seq)
        if stored is None or hmac.compare_digest(stored.hash, theirs.hash):
            return
        self.store.mark_forked(author)
        self._block(state, author, f"its HEADS hash for #{theirs.seq} differs from ours: {author[:8]} is forked")

    async def _request_missing(self, state: PeerSync) -> None:
        """Send WANTs for every author where the peer is ahead and nothing is in flight."""
        if self.store.group_id() is None:
            return  # not in a group yet; we'll look again after joining
        in_flight = {want.author for want in state.outstanding}
        admin = self.store.admin()
        # Admin first: their log carries the membership everyone else depends on.
        for author in sorted(state.their_heads, key=lambda key: (key != admin, key)):
            if author in in_flight or author in state.blocked or self.pending.holds_author(author):
                continue
            ours = self.store.head(author)
            our_seq = ours.seq if ours is not None else 0
            if state.their_heads[author].seq <= our_seq:
                continue
            if not self._may_send_want(state):
                self._retry_later(state)
                return
            want = Want(author, our_seq + 1, min(state.their_heads[author].seq, our_seq + config.MAX_WANT_RANGE))
            state.outstanding.append(want)
            state.wants_sent.append(self._clock())
            await state.peer.send(protocol.make_want(want.author, want.first, want.last))

    # --- WANT ----------------------------------------------------------------

    async def _on_want(self, state: PeerSync, author: str, first: int, last: int) -> None:
        self._check_rate(state, state.wants_received, self._want_rate_limit, "WANTs")
        raws: list[str] = []
        if first <= last and last - first < config.MAX_WANT_RANGE:
            raws = self._reply_budget(author, first, last)
            if raws:
                state.served[author] = max(state.served.get(author, 0), first + len(raws) - 1)
        else:
            # Still answer (with nothing) so the asker's request/reply order stays in step.
            logger.info("WANT from %s has a bad range %d-%d; replying with nothing", state.peer.label(), first, last)
        await state.peer.send(protocol.make_entries(raws))

    def _reply_budget(self, author: str, first: int, last: int) -> list[str]:
        """As many entries from first..last as fit in one ENTRIES (200 or 1.5 MiB)."""
        raws: list[str] = []
        size = 0
        for stored in self.store.get_range(author, first, last):
            # Measure the entry as it will appear inside ENTRIES: JSON-escaped,
            # quotes and all, so the frame really stays under 2 MiB.
            cost = len(canonical.dumps(stored.raw))
            if raws and size + cost > config.MAX_ENTRIES_REPLY_BYTES:
                break
            raws.append(stored.raw)
            size += cost
        return raws

    def _check_rate(self, state: PeerSync, times: deque[float], limit: int, what: str) -> None:
        """Count one more request in `times`; hang up if the peer is over `limit` per window."""
        now = self._clock()
        _forget_older_than(times, now - self._rate_window)
        if len(times) >= limit:
            raise PeerClosed(
                f"{state.peer.label()} sent over {limit} {what} in {self._rate_window:g} s",
                "rate_limited",
                suspicious=True,
            )
        times.append(now)

    def _may_send_want(self, state: PeerSync) -> bool:
        _forget_older_than(state.wants_sent, self._clock() - self._rate_window)
        return len(state.wants_sent) < self._want_send_limit

    def _retry_later(self, state: PeerSync) -> None:
        """We've hit our own send pace: look again once the oldest WANT leaves the window."""
        if state.retry_task is not None and not state.retry_task.done():
            return
        delay = state.wants_sent[0] + self._rate_window - self._clock()
        state.retry_task = asyncio.create_task(self._request_after(state, max(delay, 0.0)))

    async def _request_after(self, state: PeerSync, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._request_missing(state)
        except (ConnectionError, RuntimeError):
            pass

    # --- ENTRIES ---------------------------------------------------------------

    async def _on_entries(self, state: PeerSync, raws: list[str]) -> None:
        if not state.outstanding:
            logger.warning("Dropped unsolicited ENTRIES from %s", state.peer.label())
            return
        want = state.outstanding.popleft()
        self.entries_messages_received += 1
        self._evict_expired()
        added = self._apply(state, want, raws)
        if not raws:
            # The peer claimed more than it gave. Don't ask again until it sends new HEADS.
            state.their_heads.pop(want.author, None)
        if added:
            logger.info("Stored %d new %s from %s", added, "entry" if added == 1 else "entries", state.peer.label())
            await self.announce(exclude=state.peer)
        await self.resync_all()

    def _apply(self, state: PeerSync, want: Want, raws: list[str]) -> int:
        """Append entries in order, stopping at the first bad one. Returns how many were new."""
        added = 0
        members_before = self.store.members()
        for raw in raws:
            result = self.store.append_entry(raw.encode("utf-8"))
            if result.status == ADDED:
                added += 1
            elif result.status == DUPLICATE:
                continue
            elif result.status == FORKED:
                self._block(state, want.author, result.reason.message if result.reason else "fork")
                break
            elif result.reason is not None and result.reason.code == "not_member":
                if not self.pending.add(raw, state):
                    self._block(state, want.author, "the pending queue is full")
                    break
            else:
                reason = result.reason.message if result.reason else result.status
                self._block(state, want.author, f"it sent an invalid entry ({reason})")
                break
        if self.store.members() != members_before:
            added += self._retry_pending()
        return added

    def _retry_pending(self) -> int:
        """Re-try every pending entry until a pass makes no progress. Returns how many were stored."""
        total = 0
        while True:
            added_this_pass = 0
            for item in self.pending.take_all():
                result = self.store.append_entry(item.raw.encode("utf-8"))
                if result.status == ADDED:
                    added_this_pass += 1
                elif result.reason is not None and result.reason.code == "not_member":
                    self.pending.put_back(item)
                # Anything else (a duplicate, a fork, a gap) is simply dropped:
                # the entry will be fetched again in the normal way if needed.
            total += added_this_pass
            if added_this_pass == 0:
                return total

    def _evict_expired(self) -> None:
        for item in self.pending.evict_expired():
            if isinstance(item.source, PeerSync):
                self._block(item.source, item.author, "its entries waited too long for a member_add")

    # --- Chunks (home node side) ------------------------------------------------

    async def _on_chunk_want(self, state: PeerSync, chunk_ids: list[str]) -> None:
        self._check_rate(state, state.chunk_wants_received, self._chunk_want_rate_limit, "CHUNK_WANTs")
        missing = []
        for chunk_id in chunk_ids:
            chunk = self._chunks.load(chunk_id) if self._chunks is not None else None
            if chunk is None:
                missing.append(chunk_id)
            else:
                await state.peer.send(protocol.make_chunk(chunk_id, base64.b64encode(chunk).decode("ascii")))
        if missing:
            await state.peer.send(protocol.make_chunk_missing(missing))

    async def _on_chunk_push(self, state: PeerSync, chunk_id: str, data: str) -> None:
        """A member uploads a chunk of its own image post. Only the home node keeps it."""
        who = state.peer.label()
        if not self._is_home or self._chunks is None:
            logger.info("Dropped CHUNK_PUSH from %s: this isn't the home node", who)
            return
        if state.peer.peer_pubkey not in self.store.members():
            logger.warning("Dropped CHUNK_PUSH from %s: not a member", who)
            return
        try:
            chunk = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            chunk = b""
        if not hmac.compare_digest(blake2b_hex(chunk), chunk_id):
            logger.warning("Dropped CHUNK_PUSH from %s: chunk doesn't match its ID", who)
            return
        if not self._chunks.has(chunk_id) and self._chunks.usage_bytes() + len(chunk) > self.storage_quota:
            raise PeerClosed(
                f"refused a chunk from {who}: the home node is at its storage quota",
                "quota_exceeded",
            )
        self._chunks.save(chunk_id, chunk)
        await state.peer.send(protocol.make_chunk_ack(chunk_id))

    # --- Helpers -----------------------------------------------------------------

    def _block(self, state: PeerSync, author: str, why: str) -> None:
        if author not in state.blocked:
            state.blocked.add(author)
            logger.warning("No longer syncing %s with %s: %s", author[:8], state.peer.label(), why)

    def _our_heads(self) -> dict[str, tuple[int, str]]:
        return {author: (head.seq, head.hash) for author, head in self.store.heads().items()}


def _forget_older_than(times: deque[float], cutoff: float) -> None:
    while times and times[0] < cutoff:
        times.popleft()

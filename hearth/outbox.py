"""Image posts waiting for the home node to hold every one of their chunks.

The delivery guarantee: **any post that exists in a log has its chunks on the
home node.** So a post with images doesn't go into the log straight away:

1. `actions.post` encrypts the chunks, saves them locally, encrypts the post,
   and puts it here (the `outbox` table; the box is already encrypted, so
   nothing readable is stored).
2. Whenever we're connected to the home node, `push()` sends every chunk it
   hasn't acknowledged yet (CHUNK_PUSH).
3. Each CHUNK_ACK is recorded with `acknowledge()`. Once a post's chunks are
   all acknowledged, the node signs the post into its log and removes it here.

If the home node answers `quota_exceeded`, we stop pushing for a while
(`config.OUTBOX_QUOTA_RETRY`) and the post stays here, marked "home node is full".

Text-only posts never come here. Neither do posts written on the home node
itself, whose chunks are already where they need to be. (If a profile posted
images as a member and then became the home node, its node signs those
waiting posts in at start: `held_here()`.)

Joke: the departure lounge. Your post isn't boarding until all its luggage is on the plane.
"""

import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from hearth import canonical, config, protocol
from hearth.chunks import ChunkStore
from hearth.store import Store

logger = logging.getLogger(__name__)


@dataclass
class OutboxPost:
    """One waiting post. `box` is the encrypted post body."""

    row_id: int
    created: int
    box: str
    chunk_ids: list[str]
    acked_ids: list[str]

    def is_complete(self) -> bool:
        return set(self.chunk_ids) <= set(self.acked_ids)


class Outbox:
    """The posts in the outbox table, plus pushing their chunks to the home node."""

    def __init__(
        self,
        store: Store,
        chunks: ChunkStore,
        quota_retry: float = config.OUTBOX_QUOTA_RETRY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._chunks = chunks
        self._quota_retry = quota_retry
        self._clock = clock
        # When the home node last said quota_exceeded (None: it hasn't).
        self._home_full_since: float | None = None

    def add(self, created: int, box: str, chunk_ids: list[str]) -> int:
        return self._store.outbox_table.add(created, box, canonical.dumps(chunk_ids).decode("ascii"))

    def posts(self) -> list[OutboxPost]:
        return [
            OutboxPost(row_id, created, box, canonical.loads(chunk_ids), canonical.loads(acked_ids))
            for row_id, created, box, chunk_ids, acked_ids in self._store.outbox_table.rows()
        ]

    def unacked_chunk_ids(self) -> list[str]:
        """Every chunk some waiting post still needs acknowledged, without repeats, oldest post first."""
        waiting: dict[str, None] = {}
        for post in self.posts():
            for chunk_id in post.chunk_ids:
                if chunk_id not in post.acked_ids:
                    waiting[chunk_id] = None
        return list(waiting)

    def acknowledge(self, chunk_id: str) -> list[OutboxPost]:
        """Record that the home node holds `chunk_id`. Returns the posts that are now complete."""
        complete = []
        for post in self.posts():
            if chunk_id in post.chunk_ids and chunk_id not in post.acked_ids:
                post.acked_ids.append(chunk_id)
                self._store.outbox_table.set_acked(post.row_id, canonical.dumps(post.acked_ids).decode("ascii"))
            if post.is_complete():
                complete.append(post)
        return complete

    def held_here(self) -> list[OutboxPost]:
        """Waiting posts whose chunks are all on this node's disk.

        On the home node that's all a post needs. It only happens when a profile
        posted images as a member and was later run with `--role home`.
        """
        return [post for post in self.posts() if all(self._chunks.has(chunk_id) for chunk_id in post.chunk_ids)]

    def remove(self, post: OutboxPost) -> None:
        self._store.outbox_table.delete(post.row_id)

    def home_is_full(self) -> bool:
        """True while we're waiting out a quota_exceeded."""
        if self._home_full_since is None:
            return False
        return self._clock() - self._home_full_since < self._quota_retry

    def mark_home_full(self) -> None:
        self._home_full_since = self._clock()
        logger.warning("The home node is full (quota_exceeded); image posts wait in the outbox")

    async def push(self, send: Callable[[dict[str, Any]], Awaitable[None]]) -> int:
        """Send every unacknowledged chunk with `send` (a peer's send). Returns how many went out."""
        if self.home_is_full():
            return 0
        sent = 0
        for chunk_id in self.unacked_chunk_ids():
            chunk = self._chunks.load(chunk_id)
            if chunk is None:
                # We made this chunk; losing it means this post can never upload.
                logger.error("Chunk %s of a waiting post is missing from disk", chunk_id[:8])
                continue
            await send(protocol.make_chunk_push(chunk_id, base64.b64encode(chunk).decode("ascii")))
            sent += 1
        return sent

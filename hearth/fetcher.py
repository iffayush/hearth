"""Fetching image chunks from the home node, on demand.

Member nodes don't download images in the background: they fetch a post's
chunks only when someone wants to look at it (the web UI from Phase 7, or
`debug image` now). `ChunkFetcher.fetch()`:

- returns chunks we already have on disk straight away;
- otherwise asks the home node with CHUNK_WANT (up to 4 IDs at a time), waits
  up to 10 s for the answers, and tries up to 3 times;
- keeps one download per chunk ID, so two viewers asking for the same chunk at
  once share a single transfer;
- checks every CHUNK's hash against its ID before saving it (ChunkStore.save);
- paces itself under the home node's CHUNK_WANT rate limit.

`load_image()` puts it together: find the post, decrypt it, fetch the chunks,
and rebuild the image (chunks.reassemble checks every hash).

Joke: a polite fetch. It asks the home node nicely, three times at most, and
never takes a chunk it can't verify.
"""

import asyncio
import base64
import binascii
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hearth import config, protocol
from hearth.chunks import ChunkStore, ImageError, reassemble
from hearth.feed import PostError, open_post
from hearth.peer import Peer
from hearth.store import Store

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """A chunk or image we couldn't get. The message says why."""


@dataclass
class ChunkReply:
    """The answer to one requested chunk: verified bytes, or what went wrong."""

    data: bytes | None
    problem: str | None = None  # "missing" or "corrupt"


class ChunkFetcher:
    """Downloads chunks from the home node, one shared download per chunk ID."""

    def __init__(
        self,
        chunks: ChunkStore,
        home_peer: Callable[[], Peer | None],
        timeout: float = config.CHUNK_FETCH_TIMEOUT,
        attempts: int = config.CHUNK_FETCH_ATTEMPTS,
        send_limit: int = config.CHUNK_WANT_SEND_LIMIT,
        rate_window: float = config.RATE_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._chunks = chunks
        self._home_peer = home_peer
        self._timeout = timeout
        self._attempts = attempts
        self._send_limit = send_limit
        self._rate_window = rate_window
        self._clock = clock
        # chunk ID -> the download task fetching it (shared by everyone who asks).
        self._downloads: dict[str, asyncio.Task[dict[str, bytes]]] = {}
        # chunk ID -> the future the next CHUNK / CHUNK_MISSING for it will resolve.
        self._replies: dict[str, asyncio.Future[ChunkReply]] = {}
        self._sent_times: deque[float] = deque()
        # Counted for tests and, later, status.
        self.chunk_wants_sent = 0

    async def fetch(self, chunk_ids: list[str]) -> list[bytes]:
        """Every chunk in `chunk_ids`, in order. Raises FetchError if any can't be had."""
        found: dict[str, bytes] = {}
        to_download = []
        for chunk_id in dict.fromkeys(chunk_ids):
            local = self._chunks.load(chunk_id)
            if local is not None:
                found[chunk_id] = local
            elif chunk_id not in self._downloads:
                to_download.append(chunk_id)
        for start in range(0, len(to_download), config.MAX_CHUNK_WANT):
            self._start_download(to_download[start : start + config.MAX_CHUNK_WANT])
        # Grab the tasks now: a finished download removes itself from _downloads.
        tasks = {chunk_id: self._downloads[chunk_id] for chunk_id in chunk_ids if chunk_id not in found}
        for chunk_id, task in tasks.items():
            # shield: if this caller gives up, the shared download carries on for the others.
            found[chunk_id] = (await asyncio.shield(task))[chunk_id]
        return [found[chunk_id] for chunk_id in chunk_ids]

    def on_reply(self, message: dict[str, Any]) -> None:
        """Handle a CHUNK or CHUNK_MISSING from the home node."""
        if message["t"] == "CHUNK_MISSING":
            for chunk_id in message["ids"]:
                self._resolve(chunk_id, ChunkReply(None, "missing"))
            return
        chunk_id = message["id"]
        if chunk_id not in self._replies:
            logger.info("Dropped a CHUNK nobody asked for (%s)", chunk_id[:8])
            return
        try:
            chunk = base64.b64decode(message["data"], validate=True)
        except (binascii.Error, ValueError):
            chunk = b""
        # save() checks the hash against the ID before writing anything.
        if self._chunks.save(chunk_id, chunk):
            self._resolve(chunk_id, ChunkReply(chunk))
        else:
            logger.warning("Chunk %s from the home node doesn't match its ID; will ask again", chunk_id[:8])
            self._resolve(chunk_id, ChunkReply(None, "corrupt"))

    def _start_download(self, batch: list[str]) -> None:
        task = asyncio.create_task(self._download(batch))
        for chunk_id in batch:
            self._downloads[chunk_id] = task
        task.add_done_callback(self._forget_download)

    def _forget_download(self, task: asyncio.Task[dict[str, bytes]]) -> None:
        for chunk_id in [key for key, value in self._downloads.items() if value is task]:
            del self._downloads[chunk_id]

    async def _download(self, batch: list[str]) -> dict[str, bytes]:
        got: dict[str, bytes] = {}
        for attempt in range(1, self._attempts + 1):
            await self._ask_once([chunk_id for chunk_id in batch if chunk_id not in got], got, attempt)
            if len(got) == len(batch):
                return got
        raise FetchError(f"no good answer from the home node after {self._attempts} tries")

    async def _ask_once(self, wanted: list[str], got: dict[str, bytes], attempt: int) -> None:
        """One CHUNK_WANT for `wanted`; whatever arrives verified goes into `got`."""
        peer = self._home_peer()
        if peer is None:
            raise FetchError("not connected to the home node right now")
        await self._pace()
        loop = asyncio.get_running_loop()
        replies = {chunk_id: loop.create_future() for chunk_id in wanted}
        self._replies.update(replies)
        try:
            self.chunk_wants_sent += 1
            await peer.send(protocol.make_chunk_want(wanted))
            async with asyncio.timeout(self._timeout):
                for chunk_id, future in replies.items():
                    reply = await future
                    if reply.data is not None:
                        got[chunk_id] = reply.data
                    elif reply.problem == "missing":
                        raise FetchError(f"the home node doesn't have chunk {chunk_id[:8]}")
        except TimeoutError:
            logger.info("Home node didn't answer for %d chunk(s) (try %d of %d)", len(wanted), attempt, self._attempts)
        except (ConnectionError, RuntimeError):
            logger.info("Lost the home connection while fetching (try %d of %d)", attempt, self._attempts)
        finally:
            for chunk_id in replies:
                self._replies.pop(chunk_id, None)

    def _resolve(self, chunk_id: str, reply: ChunkReply) -> None:
        future = self._replies.get(chunk_id)
        if future is not None and not future.done():
            future.set_result(reply)

    async def _pace(self) -> None:
        """Stay under the home node's CHUNK_WANT limit by waiting for room in the window."""
        cutoff = self._clock() - self._rate_window
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()
        if len(self._sent_times) >= self._send_limit:
            await asyncio.sleep(self._sent_times[0] + self._rate_window - self._clock())
            self._sent_times.popleft()
        self._sent_times.append(self._clock())


async def load_image(
    store: Store,
    chunks: ChunkStore,
    group_key: bytes,
    fetcher: ChunkFetcher | None,
    author: str,
    seq: int,
    index: int,
) -> tuple[str, bytes]:
    """Find post (author, seq), get its image #index, and return (mime type, image bytes).

    With no fetcher (no node running), only chunks already on disk can be used.
    """
    stored = store.get_entry(author, seq)
    if stored is None or stored.kind != "post":
        raise FetchError(f"there's no post #{seq} by {author[:8]} here")
    try:
        images = open_post(group_key, stored.raw)["images"]
    except PostError as error:
        raise FetchError(str(error)) from error
    if not 0 <= index < len(images):
        raise FetchError(f"post #{seq} by {author[:8]} has no image #{index}")
    manifest = images[index]
    if fetcher is not None:
        pieces = await fetcher.fetch(manifest["chunks"])
    else:
        loaded = [chunks.load(chunk_id) for chunk_id in manifest["chunks"]]
        if any(piece is None for piece in loaded):
            raise FetchError("some of this image's chunks aren't stored on this node (a member's node "
                             "fetches them from home while it's running)")
        pieces = [piece for piece in loaded if piece is not None]
    try:
        return manifest["mime"], reassemble(group_key, manifest, pieces)
    except ImageError as error:
        raise FetchError(str(error)) from error

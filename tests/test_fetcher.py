"""Tests for fetcher.py: shared downloads, retries, and hash checks, against a fake home node."""

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest

from hearth import config, crypto, protocol
from hearth.chunks import ChunkStore, split_image
from hearth.fetcher import ChunkFetcher, FetchError
from hearth.store import Store

KEY = crypto.new_group_key()


class FakeHome:
    """Answers CHUNK_WANT like the home node would, with optional misbehaviour."""

    def __init__(self, chunks: dict[str, bytes], behaviour: str = "normal") -> None:
        self.chunks = chunks
        self.behaviour = behaviour
        self.wants: list[list[str]] = []
        self.fetcher: ChunkFetcher | None = None

    def label(self) -> str:
        return "home"

    async def send(self, message: dict[str, Any]) -> None:
        self.wants.append(message["ids"])
        # Reply on the next loop turn, the way a reply from the network would arrive.
        asyncio.get_running_loop().call_soon(self.reply, message["ids"])

    def reply(self, chunk_ids: list[str]) -> None:
        assert self.fetcher is not None
        if self.behaviour == "silent":
            return
        missing = [chunk_id for chunk_id in chunk_ids if chunk_id not in self.chunks]
        for chunk_id in chunk_ids:
            if chunk_id in missing:
                continue
            data = self.chunks[chunk_id]
            if self.behaviour == "corrupt_once":
                data = b"garbage" + data[7:]
                self.behaviour = "normal"
            self.fetcher.on_reply(protocol.parse_message(protocol.encode_message(
                protocol.make_chunk(chunk_id, base64.b64encode(data).decode()))))
        if missing:
            self.fetcher.on_reply(protocol.make_chunk_missing(missing))


@pytest.fixture
def chunk_store(tmp_path: Path) -> ChunkStore:
    store = Store.open(tmp_path / "hearth.db")
    (tmp_path / "chunks").mkdir()
    yield ChunkStore(tmp_path / "chunks", store)
    store.close()


def image_chunks(size: int) -> dict[str, bytes]:
    _, pieces = split_image(KEY, b"\x89PNG\r\n\x1a\n" + b"\2" * size)
    return dict(pieces)


def fetcher_for(chunk_store: ChunkStore, home: FakeHome | None, **options: Any) -> ChunkFetcher:
    fetcher = ChunkFetcher(chunk_store, lambda: home, **options)  # type: ignore[arg-type,return-value]
    if home is not None:
        home.fetcher = fetcher
    return fetcher


async def test_fetches_verifies_and_keeps_chunks(chunk_store: ChunkStore) -> None:
    remote = image_chunks(2 * config.CHUNK_BYTES)
    home = FakeHome(remote)
    fetcher = fetcher_for(chunk_store, home)
    ids = list(remote)
    assert await fetcher.fetch(ids) == [remote[chunk_id] for chunk_id in ids]
    # Now they're on disk: asking again doesn't touch the network.
    await fetcher.fetch(ids)
    assert len(home.wants) == 1


async def test_two_simultaneous_requests_cause_one_transfer(chunk_store: ChunkStore) -> None:
    remote = image_chunks(1000)
    home = FakeHome(remote)
    fetcher = fetcher_for(chunk_store, home)
    first, second = await asyncio.gather(fetcher.fetch(list(remote)), fetcher.fetch(list(remote)))
    assert first == second
    assert home.wants == [list(remote)]
    assert fetcher.chunk_wants_sent == 1


async def test_asks_for_at_most_four_chunks_at_a_time(chunk_store: ChunkStore) -> None:
    remote = image_chunks(10 * config.CHUNK_BYTES - 100)  # 10 chunks
    home = FakeHome(remote)
    fetcher = fetcher_for(chunk_store, home)
    await fetcher.fetch(list(remote))
    assert [len(batch) for batch in home.wants] == [4, 4, 2]


async def test_corrupted_chunk_is_rejected_and_asked_for_again(chunk_store: ChunkStore) -> None:
    remote = image_chunks(1000)
    home = FakeHome(remote, behaviour="corrupt_once")
    fetcher = fetcher_for(chunk_store, home)
    assert await fetcher.fetch(list(remote)) == list(remote.values())
    assert len(home.wants) == 2


async def test_missing_chunk_is_an_error(chunk_store: ChunkStore) -> None:
    home = FakeHome({})
    fetcher = fetcher_for(chunk_store, home)
    with pytest.raises(FetchError, match="doesn't have chunk"):
        await fetcher.fetch(["ab" * 32])


async def test_silent_home_gives_up_after_three_tries(chunk_store: ChunkStore) -> None:
    home = FakeHome(image_chunks(1000), behaviour="silent")
    fetcher = fetcher_for(chunk_store, home, timeout=0.05)
    with pytest.raises(FetchError, match="after 3 tries"):
        await fetcher.fetch(list(home.chunks))
    assert len(home.wants) == 3


async def test_no_home_connection_is_explained(chunk_store: ChunkStore) -> None:
    fetcher = fetcher_for(chunk_store, None)
    with pytest.raises(FetchError, match="not connected to the home node"):
        await fetcher.fetch(["ab" * 32])


async def test_unsolicited_chunk_is_dropped(chunk_store: ChunkStore) -> None:
    remote = image_chunks(1000)
    fetcher = fetcher_for(chunk_store, FakeHome(remote))
    chunk_id, chunk = next(iter(remote.items()))
    fetcher.on_reply(protocol.make_chunk(chunk_id, base64.b64encode(chunk).decode()))
    assert chunk_store.load(chunk_id) is None


async def test_pacing_waits_when_the_window_is_full(chunk_store: ChunkStore) -> None:
    remote = image_chunks(5 * config.CHUNK_BYTES)  # 6 chunks: two CHUNK_WANTs
    home = FakeHome(remote)
    fetcher = fetcher_for(chunk_store, home, send_limit=1, rate_window=0.2)
    started = asyncio.get_running_loop().time()
    await fetcher.fetch(list(remote))
    # The second CHUNK_WANT had to wait for the first to leave the 0.2 s window.
    assert asyncio.get_running_loop().time() - started >= 0.15
    assert len(home.wants) == 2

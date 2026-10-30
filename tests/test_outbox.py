"""Tests for outbox.py: image posts waiting for the home node to hold every chunk."""

from pathlib import Path
from typing import Any

import pytest

from hearth import crypto
from hearth.chunks import ChunkStore, split_image
from hearth.outbox import Outbox
from hearth.store import Store

KEY = crypto.new_group_key()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def parts(tmp_path: Path) -> tuple[Store, ChunkStore]:
    store = Store.open(tmp_path / "hearth.db")
    (tmp_path / "chunks").mkdir()
    yield store, ChunkStore(tmp_path / "chunks", store)
    store.close()


def queue_image_post(outbox: Outbox, chunks: ChunkStore, size: int = 2_500_000) -> list[str]:
    manifest, pieces = split_image(KEY, b"\x89PNG\r\n\x1a\n" + b"\1" * size)
    for chunk_id, chunk in pieces:
        chunks.save(chunk_id, chunk)
    outbox.add(1, crypto.seal_box(KEY, {"text": "cat", "images": [manifest]}), manifest["chunks"])
    return manifest["chunks"]


def test_post_completes_only_when_every_chunk_is_acked(parts: tuple[Store, ChunkStore]) -> None:
    store, chunks = parts
    outbox = Outbox(store, chunks)
    ids = queue_image_post(outbox, chunks)
    assert outbox.unacked_chunk_ids() == ids
    assert outbox.acknowledge(ids[0]) == [] and outbox.acknowledge(ids[1]) == []
    assert outbox.acknowledge(ids[1]) == []  # a duplicate ack changes nothing
    done = outbox.acknowledge(ids[2])
    assert len(done) == 1 and done[0].is_complete()
    outbox.remove(done[0])
    assert outbox.posts() == [] and outbox.unacked_chunk_ids() == []


def test_acks_survive_a_restart(tmp_path: Path, parts: tuple[Store, ChunkStore]) -> None:
    store, chunks = parts
    ids = queue_image_post(Outbox(store, chunks), chunks)
    Outbox(store, chunks).acknowledge(ids[0])
    assert Outbox(store, chunks).unacked_chunk_ids() == ids[1:]


def test_a_chunk_shared_by_two_posts_is_pushed_once(parts: tuple[Store, ChunkStore]) -> None:
    store, chunks = parts
    outbox = Outbox(store, chunks)
    ids = queue_image_post(outbox, chunks, size=100)
    outbox.add(2, "another box", ids)  # same image attached twice
    assert outbox.unacked_chunk_ids() == ids
    assert len(outbox.acknowledge(ids[0])) == 2


async def test_push_sends_every_unacked_chunk(parts: tuple[Store, ChunkStore]) -> None:
    store, chunks = parts
    outbox = Outbox(store, chunks)
    ids = queue_image_post(outbox, chunks)
    outbox.acknowledge(ids[0])
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    assert await outbox.push(send) == 2
    assert [message["id"] for message in sent] == ids[1:]
    assert all(message["t"] == "CHUNK_PUSH" for message in sent)


async def test_quota_exceeded_pauses_pushing(parts: tuple[Store, ChunkStore]) -> None:
    store, chunks = parts
    clock = Clock()
    outbox = Outbox(store, chunks, quota_retry=600, clock=clock)
    queue_image_post(outbox, chunks, size=100)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    outbox.mark_home_full()
    assert outbox.home_is_full()
    assert await outbox.push(send) == 0 and sent == []
    clock.now += 601  # ten minutes later, try again
    assert not outbox.home_is_full()
    assert await outbox.push(send) == 1


async def test_lost_chunk_is_skipped_not_fatal(parts: tuple[Store, ChunkStore]) -> None:
    store, chunks = parts
    outbox = Outbox(store, chunks)
    ids = queue_image_post(outbox, chunks)
    chunks.path_for(ids[0]).unlink()
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    assert await outbox.push(send) == 2

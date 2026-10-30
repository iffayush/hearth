"""Tests for chunks.py: magic bytes, splitting, reassembly, and the chunk store."""

import os
from pathlib import Path

import pytest

from hearth import audit, config, crypto
from hearth.chunks import ChunkStore, ImageError, detect_image_type, is_valid_manifest, reassemble, split_image
from hearth.store import Store

KEY = crypto.new_group_key()
PNG = b"\x89PNG\r\n\x1a\n"


def png_of(size: int) -> bytes:
    return PNG + os.urandom(size - len(PNG))


@pytest.mark.parametrize(
    "data, mime",
    [
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (PNG + b"rest", "image/png"),
        (b"GIF87a...", "image/gif"),
        (b"GIF89a...", "image/gif"),
        (b"RIFF\x10\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x10\x00\x00\x00WAVEfmt ", None),  # a RIFF that isn't WebP
        (b"MZ\x90\x00", None),  # a Windows executable
        (b"<svg xmlns=...", None),  # SVG can carry scripts; not allowed
        (b"", None),
    ],
)
def test_detect_image_type_by_magic_bytes(data: bytes, mime: str | None) -> None:
    assert detect_image_type(data) == mime


@pytest.mark.parametrize(
    "size, chunks",
    [(1, 1), (config.CHUNK_BYTES, 1), (config.CHUNK_BYTES + 1, 2), (config.MAX_IMAGE_BYTES, 10)],
)
def test_split_into_one_mib_pieces(size: int, chunks: int) -> None:
    data = png_of(max(size, len(PNG)))
    manifest, pieces = split_image(KEY, data)
    assert len(pieces) == len(manifest["chunks"]) == chunks
    assert manifest["mime"] == "image/png" and manifest["size"] == len(data)
    assert is_valid_manifest(manifest)
    # Chunk IDs are hashes of the *ciphertext*, and the ciphertext hides the image.
    for chunk_id, chunk in pieces:
        assert crypto.blake2b_hex(chunk) == chunk_id
        assert data[:64] not in chunk


def test_split_refuses_empty_oversized_and_fake_images() -> None:
    with pytest.raises(ImageError, match="empty"):
        split_image(KEY, b"")
    with pytest.raises(ImageError, match="10 MiB"):
        split_image(KEY, png_of(config.MAX_IMAGE_BYTES + 1))
    with pytest.raises(ImageError, match="not a JPEG"):
        split_image(KEY, b"MZ" + os.urandom(100))


def test_reassemble_byte_for_byte() -> None:
    data = png_of(2 * config.CHUNK_BYTES + 7)
    manifest, pieces = split_image(KEY, data)
    assert reassemble(KEY, manifest, [chunk for _, chunk in pieces]) == data


def test_corrupted_chunk_is_rejected_by_hash() -> None:
    manifest, pieces = split_image(KEY, png_of(1000))
    corrupted = bytearray(pieces[0][1])
    corrupted[100] ^= 0xFF
    with pytest.raises(ImageError, match="doesn't match its ID"):
        reassemble(KEY, manifest, [bytes(corrupted)])


def test_swapped_chunks_are_rejected() -> None:
    manifest, pieces = split_image(KEY, png_of(config.CHUNK_BYTES + 10))
    with pytest.raises(ImageError, match="doesn't match its ID"):
        reassemble(KEY, manifest, [pieces[1][1], pieces[0][1]])


def test_wrong_group_key_cant_rebuild_an_image() -> None:
    manifest, pieces = split_image(KEY, png_of(1000))
    with pytest.raises(ImageError, match="won't decrypt"):
        reassemble(crypto.new_group_key(), manifest, [chunk for _, chunk in pieces])


@pytest.mark.parametrize(
    "change",
    [
        {"mime": "image/svg+xml"},
        {"size": 0},
        {"size": True},
        {"size": config.MAX_IMAGE_BYTES + 1},
        {"sha": "nope"},
        {"chunks": []},  # one chunk too few for its size
        {"chunks": ["AB" * 32]},
        {"extra": 1},
    ],
)
def test_malformed_manifests_are_refused(change: dict[str, object]) -> None:
    manifest, _ = split_image(KEY, png_of(100))
    assert not is_valid_manifest({**manifest, **change})


# --- ChunkStore ---------------------------------------------------------------------


@pytest.fixture
def chunk_store(tmp_path: Path) -> ChunkStore:
    store = Store.open(tmp_path / "hearth.db")
    (tmp_path / "chunks").mkdir()
    yield ChunkStore(tmp_path / "chunks", store)
    store.close()


def test_save_load_and_usage(chunk_store: ChunkStore) -> None:
    _, pieces = split_image(KEY, png_of(5000))
    chunk_id, chunk = pieces[0]
    assert chunk_store.save(chunk_id, chunk)
    assert chunk_store.save(chunk_id, chunk)  # saving again is harmless
    assert chunk_store.load(chunk_id) == chunk
    assert chunk_store.path_for(chunk_id) == chunk_store.root / chunk_id[:2] / chunk_id
    assert chunk_store.usage_bytes() == len(chunk)


def test_save_refuses_a_chunk_that_doesnt_match_its_id(chunk_store: ChunkStore) -> None:
    assert not chunk_store.save("00" * 32, b"some other bytes")
    assert chunk_store.load("00" * 32) is None
    assert chunk_store.usage_bytes() == 0


def test_damaged_file_on_disk_is_not_trusted(chunk_store: ChunkStore) -> None:
    _, pieces = split_image(KEY, png_of(500))
    chunk_id, chunk = pieces[0]
    chunk_store.save(chunk_id, chunk)
    chunk_store.path_for(chunk_id).write_bytes(b"bit rot")
    assert chunk_store.load(chunk_id) is None


@pytest.mark.parametrize("sneaky_id", ["../../etc/passwd", "..\\..\\windows", "AB" * 32, "ab" * 31, "ab" * 32 + "/x"])
def test_path_traversal_with_a_fake_chunk_id_is_refused(chunk_store: ChunkStore, sneaky_id: str) -> None:
    with pytest.raises(ValueError):
        chunk_store.path_for(sneaky_id)


# --- debug verify's chunk check (audit.verify_chunks) ---------------------------------


def test_verify_chunks_finds_damaged_and_missing_files(chunk_store: ChunkStore) -> None:
    _, pieces = split_image(KEY, png_of(3 * config.CHUNK_BYTES))
    for chunk_id, chunk in pieces:
        chunk_store.save(chunk_id, chunk)
    assert audit.verify_chunks(chunk_store) == {"checked": 3, "problems": []}

    (rotten, _), (lost, _), _ = pieces
    chunk_store.path_for(rotten).write_bytes(b"bit rot")
    chunk_store.path_for(lost).unlink()
    found = {problem["chunk"]: problem["problem"] for problem in audit.verify_chunks(chunk_store)["problems"]}
    assert found == {rotten: "damaged", lost: "missing"}

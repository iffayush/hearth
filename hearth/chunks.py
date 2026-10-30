"""Images as encrypted chunks: type checks, splitting, reassembly, and storage on disk.

Posting an image:
1. `detect_image_type()` checks the first bytes (JPEG, PNG, WebP or GIF only;
   the file name is never trusted). Max 10 MiB.
2. `split_image()` cuts it into 1 MiB pieces, encrypts each with the group
   key, and names each chunk by the BLAKE2b-256 of its *ciphertext*. The post
   carries a manifest: {"mime", "size", "sha" (of the whole plaintext image),
   "chunks" (IDs in order)}.

Viewing it: fetch the chunks, then `reassemble()` checks each chunk's hash
against its ID, decrypts, joins, and checks the whole-image hash and size.

`ChunkStore` keeps chunks as files at `chunks/<first 2 hex>/<chunk id>`, plus a
row in the `chunks` table (for the quota). Paths are built only from validated
64-hex IDs and double-checked to stay inside `chunks/`.

Joke: we chop your cat photo into encrypted cubes. The cat is fine. The cat is
always fine.
"""

import hmac
import logging
import math
from pathlib import Path
from typing import Any

from hearth import config
from hearth.crypto import DecryptError, blake2b_hex, decrypt_chunk, encrypt_chunk
from hearth.keyfile import write_private_file
from hearth.entry import is_hex64
from hearth.store import Store

logger = logging.getLogger(__name__)

MANIFEST_FIELDS = frozenset({"mime", "size", "sha", "chunks"})


class ImageError(ValueError):
    """An image we won't post or can't rebuild. The message says why."""


def detect_image_type(data: bytes) -> str | None:
    """The MIME type from the file's magic bytes, or None if it isn't an allowed image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    # WebP is a RIFF container: "RIFF", a 4-byte size, then "WEBP".
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def split_image(group_key: bytes, data: bytes) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """Check, split and encrypt an image. Returns (manifest, [(chunk_id, ciphertext), ...])."""
    if not data:
        raise ImageError("the image is empty")
    if len(data) > config.MAX_IMAGE_BYTES:
        raise ImageError(f"the image is {len(data) / 2**20:.1f} MiB; the limit is 10 MiB")
    mime = detect_image_type(data)
    if mime is None:
        raise ImageError("that's not a JPEG, PNG, WebP or GIF (judging by its first bytes, not its name)")
    chunks = []
    for start in range(0, len(data), config.CHUNK_BYTES):
        ciphertext = encrypt_chunk(group_key, data[start : start + config.CHUNK_BYTES])
        chunks.append((blake2b_hex(ciphertext), ciphertext))
    manifest = {"mime": mime, "size": len(data), "sha": blake2b_hex(data), "chunks": [cid for cid, _ in chunks]}
    return manifest, chunks


def is_valid_manifest(manifest: Any) -> bool:
    """Shape check for a manifest that came out of a decrypted post."""
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        return False
    size, chunk_ids = manifest["size"], manifest["chunks"]
    if type(size) is not int or not 1 <= size <= config.MAX_IMAGE_BYTES:
        return False
    if manifest["mime"] not in config.IMAGE_MIME_TYPES or not is_hex64(manifest["sha"]):
        return False
    # The number of chunks must be exactly what that size needs.
    return (
        isinstance(chunk_ids, list)
        and len(chunk_ids) == math.ceil(size / config.CHUNK_BYTES)
        and all(is_hex64(chunk_id) for chunk_id in chunk_ids)
    )


def reassemble(group_key: bytes, manifest: dict[str, Any], chunks: list[bytes]) -> bytes:
    """Verify, decrypt and join an image's chunks, then check the whole-image hash."""
    if len(chunks) != len(manifest["chunks"]):
        raise ImageError("wrong number of chunks for this image")
    pieces = []
    for number, (chunk_id, chunk) in enumerate(zip(manifest["chunks"], chunks), start=1):
        if not hmac.compare_digest(blake2b_hex(chunk), chunk_id):
            raise ImageError(f"chunk {number} doesn't match its ID (corrupted or swapped)")
        try:
            pieces.append(decrypt_chunk(group_key, chunk))
        except DecryptError as error:
            raise ImageError(f"chunk {number} won't decrypt: {error}") from error
    data = b"".join(pieces)
    if len(data) != manifest["size"] or not hmac.compare_digest(blake2b_hex(data), manifest["sha"]):
        raise ImageError("the rebuilt image doesn't match its hash")
    if detect_image_type(data) != manifest["mime"]:
        raise ImageError("the rebuilt image isn't the type its manifest says")
    return data


class ChunkStore:
    """Encrypted chunks on disk under `root`, with their sizes recorded in the store."""

    def __init__(self, root: Path, store: Store) -> None:
        self.root = root
        self._store = store

    def path_for(self, chunk_id: str) -> Path:
        """The file for `chunk_id`. Raises ValueError for anything but a 64-hex ID."""
        if not is_hex64(chunk_id):
            raise ValueError("chunk IDs are 64 lowercase hex characters")
        path = self.root / chunk_id[:2] / chunk_id
        # Belt and braces: the ID is plain hex, but confirm the path stays inside chunks/.
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("chunk path escapes the chunks folder")
        return path

    def has(self, chunk_id: str) -> bool:
        return self.path_for(chunk_id).exists()

    def save(self, chunk_id: str, chunk: bytes) -> bool:
        """Store a chunk if its hash matches its ID. Returns False (and stores nothing) if not."""
        if len(chunk) > config.MAX_CHUNK_CIPHERTEXT_BYTES or not hmac.compare_digest(blake2b_hex(chunk), chunk_id):
            return False
        path = self.path_for(chunk_id)
        if not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_private_file(path, chunk)
        self._store.chunk_table.add(chunk_id, len(chunk))
        return True

    def load(self, chunk_id: str) -> bytes | None:
        """The chunk's bytes, or None if we don't have it (or our copy is damaged)."""
        path = self.path_for(chunk_id)
        try:
            with path.open("rb") as handle:
                chunk = handle.read(config.MAX_CHUNK_CIPHERTEXT_BYTES + 1)
        except FileNotFoundError:
            return None
        if not hmac.compare_digest(blake2b_hex(chunk), chunk_id):
            logger.warning("Chunk %s on disk doesn't match its ID; ignoring our copy", chunk_id[:8])
            return None
        return chunk

    def usage_bytes(self) -> int:
        return self._store.chunk_table.usage()

    def ids(self) -> list[str]:
        """Every chunk ID the database says we hold."""
        return self._store.chunk_table.ids()

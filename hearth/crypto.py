"""Group encryption: one shared key, and SecretBox for everything it protects.

Every member holds the same 32-byte group key. It encrypts:

- each entry's `box` (post or profile content): canonical JSON, then
  SecretBox(group_key).encrypt() with a fresh random 24-byte nonce, then base64;
- each image chunk: the same SecretBox call on raw bytes (no base64; chunks
  travel as files and, on the wire, inside base64 fields).

Anyone without the key sees only ciphertext: metadata like author, seq, kind and
timestamps stays visible (by design, see the spec's "accepted leaks"), content
never does.

SecretBox is XSalsa20-Poly1305: it both encrypts and authenticates, so a single
flipped bit anywhere makes decryption fail instead of returning garbage.

Joke: one key to rule them all, and in the SecretBox bind them.
"""

import base64
import binascii
import hashlib
from typing import Any

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as random_bytes

from hearth import canonical, config


class DecryptError(Exception):
    """Ciphertext that won't open: wrong key, tampered, or not ciphertext at all."""


def new_group_key() -> bytes:
    return random_bytes(config.GROUP_KEY_BYTES)


def seal_box(group_key: bytes, value: dict[str, Any]) -> str:
    """Encrypt a JSON object for an entry's `box` field."""
    # encrypt() picks a random nonce and puts it in front of the ciphertext.
    sealed = SecretBox(group_key).encrypt(canonical.dumps(value))
    return base64.b64encode(bytes(sealed)).decode("ascii")


def open_box(group_key: bytes, box: str) -> Any:
    """Decrypt and strictly parse a `box`. Raises DecryptError if anything is off."""
    try:
        sealed = base64.b64decode(box, validate=True)
        plaintext = SecretBox(group_key).decrypt(sealed)
        return canonical.loads(plaintext)
    except (binascii.Error, ValueError, CryptoError) as error:
        # ValueError covers both bad base64 and CanonicalError from the parser.
        raise DecryptError("can't open this box: wrong group key, or damaged") from error


def encrypt_chunk(group_key: bytes, piece: bytes) -> bytes:
    """Encrypt one image piece. The result (nonce + ciphertext) is what's stored and sent."""
    return bytes(SecretBox(group_key).encrypt(piece))


def decrypt_chunk(group_key: bytes, chunk: bytes) -> bytes:
    try:
        return SecretBox(group_key).decrypt(chunk)
    except (CryptoError, ValueError) as error:
        raise DecryptError("can't decrypt this chunk: wrong group key, or damaged") from error


def blake2b_hex(data: bytes) -> str:
    """BLAKE2b-256 as 64 hex characters: chunk IDs and whole-image hashes."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()

"""Passphrase-encrypted key files (`identity.key` and, from Phase 5, `group.key`).

File layout, all fields back to back:

    tag        e.g. b"hearth-id-v1\\n" (says which kind of key file this is)
    salt       16 random bytes
    opslimit   4-byte big-endian Argon2id work factor
    memlimit   8-byte big-endian Argon2id memory, in bytes
    box        SecretBox ciphertext (nonce included) of the secret plaintext

The SecretBox key is Argon2id(passphrase, salt, opslimit, memlimit). The stored
limits drive an expensive computation, so before running the KDF we accept only
libsodium's INTERACTIVE and MODERATE pairs. Otherwise a tampered file could ask
for 1 TiB of RAM and take the node down before the passphrase is even checked.

Joke: Argon2id is a lock that makes burglars do push-ups before every guess.
"""

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from nacl import pwhash
from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from hearth import config

# The only (opslimit, memlimit) pairs we'll ever run the KDF with.
KDF_PARAMS: dict[str, tuple[int, int]] = {
    "interactive": (pwhash.argon2id.OPSLIMIT_INTERACTIVE, pwhash.argon2id.MEMLIMIT_INTERACTIVE),
    "moderate": (pwhash.argon2id.OPSLIMIT_MODERATE, pwhash.argon2id.MEMLIMIT_MODERATE),
}

OPSLIMIT_BYTES = 4
MEMLIMIT_BYTES = 8


class KeyFileError(Exception):
    """A key file could not be read. `reason` is a short code, the message is for humans."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class KeyFileHeader:
    """The unencrypted fields at the start of a key file."""

    tag: bytes
    salt: bytes
    opslimit: int
    memlimit: int


def write_keyfile(path: Path, tag: bytes, secret: bytes, passphrase: str, kdf: str) -> None:
    """Encrypt `secret` with a key derived from `passphrase` and write it atomically."""
    if kdf not in KDF_PARAMS:
        raise ValueError(f"unknown KDF setting {kdf!r}; choose one of {sorted(KDF_PARAMS)}")
    opslimit, memlimit = KDF_PARAMS[kdf]
    salt = secrets.token_bytes(config.KEYFILE_SALT_BYTES)
    box = SecretBox(_derive_key(passphrase, salt, opslimit, memlimit))
    data = (
        tag
        + salt
        + opslimit.to_bytes(OPSLIMIT_BYTES, "big")
        + memlimit.to_bytes(MEMLIMIT_BYTES, "big")
        + box.encrypt(secret)
    )
    write_private_file(path, data)


def read_keyfile(path: Path, tag: bytes, passphrase: str) -> bytes:
    """Return the decrypted secret, or raise KeyFileError saying exactly what's wrong."""
    data = _read_small_file(path)
    header, ciphertext = parse_header(data, tag)
    key = _derive_key(passphrase, header.salt, header.opslimit, header.memlimit)
    try:
        return SecretBox(key).decrypt(ciphertext)
    except CryptoError as error:
        # Wrong passphrase and a corrupted file look identical, by design.
        raise KeyFileError(
            "wrong_passphrase",
            f"{path.name}: wrong passphrase, or the file is damaged.",
        ) from error


def parse_header(data: bytes, tag: bytes) -> tuple[KeyFileHeader, bytes]:
    """Split a key file into its header and ciphertext, checking tag and KDF limits.

    Never runs the KDF, so it's cheap to call on untrusted files.
    """
    if not data.startswith(tag):
        raise KeyFileError(
            "wrong_tag",
            f"this is not a {tag.decode().strip()} file (was it swapped with another key file?)",
        )
    position = len(tag)
    salt = data[position : position + config.KEYFILE_SALT_BYTES]
    position += config.KEYFILE_SALT_BYTES
    opslimit = int.from_bytes(data[position : position + OPSLIMIT_BYTES], "big")
    position += OPSLIMIT_BYTES
    memlimit = int.from_bytes(data[position : position + MEMLIMIT_BYTES], "big")
    position += MEMLIMIT_BYTES
    ciphertext = data[position:]
    if len(salt) != config.KEYFILE_SALT_BYTES or not ciphertext:
        raise KeyFileError("truncated", "key file is truncated")
    if (opslimit, memlimit) not in KDF_PARAMS.values():
        raise KeyFileError(
            "bad_kdf_params",
            f"key file asks for unsupported KDF limits (opslimit={opslimit}, "
            f"memlimit={memlimit}); refusing to run them.",
        )
    return KeyFileHeader(tag, salt, opslimit, memlimit), ciphertext


def kdf_name(path: Path, tag: bytes) -> str:
    """Which KDF setting ("interactive" or "moderate") an existing key file uses. No decryption."""
    header, _ = parse_header(_read_small_file(path), tag)
    for name, params in KDF_PARAMS.items():
        if params == (header.opslimit, header.memlimit):
            return name
    # parse_header only accepts the pairs in KDF_PARAMS, so this can't happen.
    raise KeyFileError("bad_kdf_params", f"{path.name} uses unknown KDF limits")


def write_private_file(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically, readable only by the owner.

    Writes a temp file in the same folder, then os.replace()s it over the
    target, so a crash leaves either the old file or the new one, never half.
    """
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    # O_EXCL: never write through a file (or symlink) someone placed there first.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _read_small_file(path: Path) -> bytes:
    # Check the size before reading, so a huge file is rejected unread.
    if path.stat().st_size > config.KEYFILE_MAX_BYTES:
        raise KeyFileError(
            "too_large",
            f"{path.name} is larger than {config.KEYFILE_MAX_BYTES} bytes; not a Hearth key file.",
        )
    with path.open("rb") as handle:
        # Read one byte past the limit in case the file grew since stat().
        data = handle.read(config.KEYFILE_MAX_BYTES + 1)
    if len(data) > config.KEYFILE_MAX_BYTES:
        raise KeyFileError("too_large", f"{path.name} is too large")
    return data


def time_kdf(kdf: str) -> float:
    """Seconds one key derivation takes with this setting here (`debug bench-kdf`)."""
    opslimit, memlimit = KDF_PARAMS[kdf]
    started = time.perf_counter()
    _derive_key("not a real passphrase", secrets.token_bytes(pwhash.argon2id.SALTBYTES), opslimit, memlimit)
    return time.perf_counter() - started


def _derive_key(passphrase: str, salt: bytes, opslimit: int, memlimit: int) -> bytes:
    # Looked up through the module each call so tests can check it's never reached.
    return pwhash.argon2id.kdf(
        SecretBox.KEY_SIZE,
        passphrase.encode("utf-8"),
        salt,
        opslimit=opslimit,
        memlimit=memlimit,
    )

"""Tests for identity.py: two key pairs, saved encrypted, loaded back intact."""

from pathlib import Path

import pytest

from hearth import config
from hearth.identity import Identity, verify_signature
from hearth.keyfile import KeyFileError

from conftest import make_identity


def test_create_then_load_gives_same_keys(tmp_path: Path) -> None:
    created, data_dir = make_identity(tmp_path, "alice")
    loaded = Identity.load(data_dir / config.IDENTITY_FILE, "hunter2")
    assert loaded.pubkey == created.pubkey
    assert loaded.encryption_pubkey == created.encryption_pubkey


def test_signing_and_encryption_keys_are_independent(alice: Identity) -> None:
    assert alice.pubkey != alice.encryption_pubkey
    assert len(alice.pubkey) == 64 and len(alice.encryption_pubkey) == 64


def test_key_file_contains_no_plaintext_keys(tmp_path: Path) -> None:
    created, data_dir = make_identity(tmp_path, "alice")
    data = (data_dir / config.IDENTITY_FILE).read_bytes()
    assert created.signing_key.encode() not in data
    assert created.signing_key.encode().hex().encode() not in data


def test_wrong_passphrase_fails_cleanly(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    with pytest.raises(KeyFileError) as caught:
        Identity.load(data_dir / config.IDENTITY_FILE, "password123")
    assert caught.value.reason == "wrong_passphrase"


def test_create_refuses_to_overwrite(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    with pytest.raises(FileExistsError):
        Identity.create(data_dir / config.IDENTITY_FILE, "hunter2", kdf="interactive")


def test_signature_verifies(alice: Identity) -> None:
    signature = alice.sign(b"hello")
    assert verify_signature(alice.pubkey, b"hello", signature)
    assert not verify_signature(alice.pubkey, b"hellO", signature)


def test_bob_cannot_pretend_to_be_alice(alice: Identity, bob: Identity) -> None:
    signature = bob.sign(b"I am alice, honest")
    assert not verify_signature(alice.pubkey, b"I am alice, honest", signature)


def test_repr_never_shows_private_keys(alice: Identity) -> None:
    text = repr(alice)
    assert alice.signing_key.encode().hex() not in text
    assert alice.short_id in text

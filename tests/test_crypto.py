"""Tests for crypto.py: boxes and chunks under the group key."""

import base64

import pytest
from nacl.secret import SecretBox

from hearth import crypto
from hearth.crypto import DecryptError


def test_box_round_trip() -> None:
    key = crypto.new_group_key()
    value = {"text": "chai ☕", "images": []}
    assert crypto.open_box(key, crypto.seal_box(key, value)) == value


def test_same_plaintext_gives_different_boxes() -> None:
    # A fresh random nonce every time: nobody can tell two posts say the same thing.
    key = crypto.new_group_key()
    assert crypto.seal_box(key, {"text": "hi"}) != crypto.seal_box(key, {"text": "hi"})


def test_wrong_group_key_fails_to_decrypt() -> None:
    box = crypto.seal_box(crypto.new_group_key(), {"text": "members only"})
    with pytest.raises(DecryptError, match="wrong group key"):
        crypto.open_box(crypto.new_group_key(), box)


@pytest.mark.parametrize("damage", ["flip", "truncate", "not_base64"])
def test_damaged_box_fails_cleanly(damage: str) -> None:
    key = crypto.new_group_key()
    sealed = bytearray(base64.b64decode(crypto.seal_box(key, {"text": "hi"})))
    if damage == "flip":
        sealed[-1] ^= 1
        box = base64.b64encode(sealed).decode()
    elif damage == "truncate":
        box = base64.b64encode(sealed[:10]).decode()
    else:
        box = "this is not base64!"
    with pytest.raises(DecryptError):
        crypto.open_box(key, box)


def test_box_that_decrypts_to_non_json_is_refused() -> None:
    key = crypto.new_group_key()
    sneaky = base64.b64encode(bytes(SecretBox(key).encrypt(b"{1.5: nope"))).decode()
    with pytest.raises(DecryptError):
        crypto.open_box(key, sneaky)


def test_chunk_round_trip_and_wrong_key() -> None:
    key = crypto.new_group_key()
    chunk = crypto.encrypt_chunk(key, b"\x89PNG pixels")
    assert b"pixels" not in chunk
    assert crypto.decrypt_chunk(key, chunk) == b"\x89PNG pixels"
    with pytest.raises(DecryptError):
        crypto.decrypt_chunk(crypto.new_group_key(), chunk)


def test_blake2b_is_32_bytes_of_hex() -> None:
    digest = crypto.blake2b_hex(b"hearth")
    assert len(digest) == 64 and digest == crypto.blake2b_hex(b"hearth") != crypto.blake2b_hex(b"Hearth")

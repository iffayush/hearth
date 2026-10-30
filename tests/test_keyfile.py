"""Tests for keyfile.py: the tagged, passphrase-encrypted key file format."""

import os
from pathlib import Path

import pytest
from nacl import pwhash

from hearth import config, keyfile
from hearth.keyfile import KeyFileError

SECRET = b"the secret recipe is more cardamom"


def write_test_file(path: Path, tag: bytes = config.IDENTITY_TAG) -> None:
    keyfile.write_keyfile(path, tag, SECRET, "hunter2", kdf="interactive")


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "test.key"
    write_test_file(path)
    assert keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2") == SECRET


def test_moderate_kdf_round_trip(tmp_path: Path) -> None:
    # The default; slower (256 MiB), so it gets just this one test.
    path = tmp_path / "test.key"
    keyfile.write_keyfile(path, config.IDENTITY_TAG, SECRET, "hunter2", kdf="moderate")
    assert keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2") == SECRET


def test_file_layout_matches_spec(tmp_path: Path) -> None:
    path = tmp_path / "test.key"
    write_test_file(path)
    data = path.read_bytes()
    tag_length = len(config.IDENTITY_TAG)
    assert data.startswith(b"hearth-id-v1\n")
    opslimit = int.from_bytes(data[tag_length + 16 : tag_length + 20], "big")
    memlimit = int.from_bytes(data[tag_length + 20 : tag_length + 28], "big")
    assert (opslimit, memlimit) == keyfile.KDF_PARAMS["interactive"]
    assert SECRET not in data


def test_wrong_passphrase_fails_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "test.key"
    write_test_file(path)
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter3")
    assert caught.value.reason == "wrong_passphrase"


def test_swapped_key_file_is_rejected_by_tag(tmp_path: Path) -> None:
    group_key_path = tmp_path / "group.key"
    write_test_file(group_key_path, tag=config.GROUP_KEY_TAG)
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(group_key_path, config.IDENTITY_TAG, "hunter2")
    assert caught.value.reason == "wrong_tag"


def test_out_of_range_kdf_params_rejected_without_running_kdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "test.key"
    write_test_file(path)
    data = bytearray(path.read_bytes())
    memlimit_start = len(config.IDENTITY_TAG) + 16 + 4
    # Ask for 1 TiB of RAM: exactly what a malicious file would do.
    data[memlimit_start : memlimit_start + 8] = (2**40).to_bytes(8, "big")
    path.write_bytes(bytes(data))

    def kdf_must_not_run(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("the KDF ran on untrusted parameters")

    monkeypatch.setattr(pwhash.argon2id, "kdf", kdf_must_not_run)
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2")
    assert caught.value.reason == "bad_kdf_params"


def test_mixed_kdf_pair_is_rejected(tmp_path: Path) -> None:
    # Each number is individually "allowed", but the pair isn't one of the two.
    path = tmp_path / "test.key"
    write_test_file(path)
    data = bytearray(path.read_bytes())
    ops_start = len(config.IDENTITY_TAG) + 16
    data[ops_start : ops_start + 4] = pwhash.argon2id.OPSLIMIT_MODERATE.to_bytes(4, "big")
    path.write_bytes(bytes(data))
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2")
    assert caught.value.reason == "bad_kdf_params"


def test_large_file_rejected_unread(tmp_path: Path) -> None:
    path = tmp_path / "huge.key"
    path.write_bytes(config.IDENTITY_TAG + b"\0" * 5000)
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2")
    assert caught.value.reason == "too_large"


def test_truncated_file_rejected(tmp_path: Path) -> None:
    path = tmp_path / "short.key"
    path.write_bytes(config.IDENTITY_TAG + b"\1\2\3")
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2")
    assert caught.value.reason == "truncated"


def test_flipped_ciphertext_byte_fails(tmp_path: Path) -> None:
    path = tmp_path / "test.key"
    write_test_file(path)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))
    with pytest.raises(KeyFileError) as caught:
        keyfile.read_keyfile(path, config.IDENTITY_TAG, "hunter2")
    assert caught.value.reason == "wrong_passphrase"


def test_unknown_kdf_name_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown KDF"):
        keyfile.write_keyfile(tmp_path / "x.key", config.IDENTITY_TAG, SECRET, "pw", kdf="sensitive")


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    write_test_file(tmp_path / "test.key")
    write_test_file(tmp_path / "test.key")  # overwriting works too
    assert [p.name for p in tmp_path.iterdir()] == ["test.key"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions only")
def test_key_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "test.key"
    write_test_file(path)
    assert path.stat().st_mode & 0o777 == 0o600

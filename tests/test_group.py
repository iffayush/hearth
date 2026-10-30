"""Tests for group.py: the passphrase-encrypted group key file."""

from pathlib import Path

import pytest

from hearth import config, crypto, group, keyfile
from hearth.keyfile import KeyFileError

from conftest import make_identity


# --- group.py ----------------------------------------------------------------------


def test_group_key_file_round_trip(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    key = crypto.new_group_key()
    group.save_group_key(data_dir, key, "hunter2")
    assert group.load_group_key(data_dir, "hunter2") == key
    raw = (data_dir / config.GROUP_KEY_FILE).read_bytes()
    assert raw.startswith(b"hearth-group-v1\n") and key not in raw


def test_group_key_uses_the_identity_files_kdf_strength(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")  # made with "interactive"
    group.save_group_key(data_dir, crypto.new_group_key(), "hunter2")
    assert keyfile.kdf_name(data_dir / config.GROUP_KEY_FILE, config.GROUP_KEY_TAG) == "interactive"


def test_no_group_key_yet_is_none(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    assert group.load_group_key(data_dir, "hunter2") is None


def test_group_key_is_never_overwritten(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    group.save_group_key(data_dir, crypto.new_group_key(), "hunter2")
    with pytest.raises(FileExistsError):
        group.save_group_key(data_dir, crypto.new_group_key(), "hunter2")


def test_identity_file_swapped_in_as_group_key_is_refused(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    (data_dir / config.GROUP_KEY_FILE).write_bytes((data_dir / config.IDENTITY_FILE).read_bytes())
    with pytest.raises(KeyFileError) as caught:
        group.load_group_key(data_dir, "hunter2")
    assert caught.value.reason == "wrong_tag"


def test_wrong_passphrase_for_group_key(tmp_path: Path) -> None:
    _, data_dir = make_identity(tmp_path, "alice")
    group.save_group_key(data_dir, crypto.new_group_key(), "hunter2")
    with pytest.raises(KeyFileError) as caught:
        group.load_group_key(data_dir, "hunter3")
    assert caught.value.reason == "wrong_passphrase"

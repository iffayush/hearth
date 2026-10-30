"""The group key file, `<data-dir>/group.key`, and who counts as a member.

The group key is the one secret every member shares (see crypto.py). It lives
in its own key file, in the same tagged, passphrase-encrypted format as
`identity.key` (keyfile.py), with the tag `hearth-group-v1` so the two files
can never be mixed up, and the same passphrase and KDF strength as the identity.

Membership itself is the admin's log: `group_create` makes the admin a
member, each `member_add` adds one more (store.py keeps the list). `is_member`
is the question every connection asks after its handshake. New members get
the group key sealed inside INVITE_OK (invite.py).

Joke: the group key is the spare key under the doormat, except the doormat is
Argon2id and the burglar has to do push-ups first.
"""

from pathlib import Path

from hearth import config, keyfile
from hearth.store import Store


def is_member(store: Store, pubkey: str) -> bool:
    """True for the group's admin and anyone the admin has added."""
    return pubkey == store.admin() or pubkey in store.members()


def group_key_path(data_dir: Path) -> Path:
    return data_dir / config.GROUP_KEY_FILE


def save_group_key(data_dir: Path, group_key: bytes, passphrase: str) -> None:
    """Write group.key, encrypted like identity.key and with the same KDF strength.

    Refuses to overwrite: replacing the group key would lock you out of every post.
    """
    path = group_key_path(data_dir)
    if path.exists():
        raise FileExistsError(f"{path} already exists; refusing to replace a group key")
    if len(group_key) != config.GROUP_KEY_BYTES:
        raise ValueError(f"a group key is {config.GROUP_KEY_BYTES} bytes")
    kdf = keyfile.kdf_name(data_dir / config.IDENTITY_FILE, config.IDENTITY_TAG)
    keyfile.write_keyfile(path, config.GROUP_KEY_TAG, group_key, passphrase, kdf)


def load_group_key(data_dir: Path, passphrase: str) -> bytes | None:
    """The group key, or None if this node hasn't joined a group yet."""
    path = group_key_path(data_dir)
    if not path.exists():
        return None
    group_key = keyfile.read_keyfile(path, config.GROUP_KEY_TAG, passphrase)
    if len(group_key) != config.GROUP_KEY_BYTES:
        raise keyfile.KeyFileError("bad_contents", f"{path.name} decrypted but doesn't hold a group key")
    return group_key

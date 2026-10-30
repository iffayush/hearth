# Phase 1: Identity and signed entries

> **Changed in Phase 2:** `debug sign` now appends to the store and has no `--group`/`--out` flags, and `debug verify` checks the whole store. The commands below describe Phase 1 as built; see `phase-2.md` for the current ones.

## What was built

Phase 1 gives every member a cryptographic identity and lets them sign log entries that anyone can check. There's no network, database or group yet.

**Identity.** `python -m hearth init` calls `Identity.create()` in `identity.py`, which generates two independent key pairs: an Ed25519 signing key (its hex public key is your account ID) and an X25519 encryption key (used in Phase 6 to receive the group key). Both private keys go into a small canonical JSON object, which `keyfile.write_keyfile()` encrypts.

**Key files.** `keyfile.py` defines the on-disk format: tag, 16-byte salt, opslimit, memlimit, then a SecretBox ciphertext keyed by Argon2id(passphrase). The function that matters most is `parse_header()`: it checks the tag (so `group.key` can't be passed off as `identity.key`) and rejects any KDF limits other than libsodium's INTERACTIVE or MODERATE pairs *before* running the KDF. `_read_small_file()` refuses files over 4 KiB unread. `write_private_file()` writes a temp file with mode 0600 and `os.replace`s it, so a crash never leaves half a key.

**Canonical JSON.** `canonical.py` has `dumps()` (sorted keys, no spaces, UTF-8) and the strict `loads()`, which rejects floats, NaN/Infinity, duplicate keys and lone surrogates. `is_int()` uses `type(x) is int`, because `isinstance(True, int)` is true.

**Entries.** In `entry.py`, `create_entry()` fills in `seq` and `prev` from the author's `Head`, computes `signed_bytes()` (prefix `hearth/entry/v1\n` + canonical JSON minus `sig`) and signs it. `entry_hash()` is BLAKE2b-256 of the same bytes. `validate_entry()` is a pure function and the heart of the phase: shape → size → version → group → kind → signature → body → authority → chain. Each check returns a `Reason(code, message)`, so a failure always says *why* (`bad_signature`, `bad_seq`, `bad_prev`, `not_member`...).

**Lock.** `lockfile.DataDirLock` takes an OS lock on `hearth.lock`; `init` and `debug sign` hold it while they touch key files.

**Data flow for `debug sign`:** CLI → `DataDirLock` → read passphrase → `Identity.load()` → `keyfile.read_keyfile()` → `create_entry()` → `validate_entry()` self-check → print canonical JSON.

Old data directories: none exist yet, so there's nothing to delete.

## Try it yourself

From the project folder, with the virtualenv active (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` elsewhere):

```
python -m pytest
```
Expect about `97 passed, 2 skipped` (the skips are POSIX-only permission tests on Windows).

```
python -m hearth init --profile alice
```
It asks for a passphrase twice, then prints `Your account ID: <64 hex chars>`. Look in `~/.hearth/alice/`: `identity.key` (253 bytes of noise starting with `hearth-id-v1`), `config.json`, `hearth.lock`.

```
python -m hearth debug sign "hello hearth" --profile alice --group abababababababababababababababababababababababababababababababab --out entry.json
```
Prints `hash: … (self-check passed)` and one line of JSON with `"seq":1,"prev":null` and a 128-hex `sig`.

```
python -m hearth debug verify entry.json --group abababababababababababababababababababababababababababababababab
```
Prints `valid: entry #1 by <8 hex>, hash …`.

## Poke it

1. **Tamper with a post.** Open `entry.json` in an editor, change `hello` to `jello`, save, and run `debug verify` again: `rejected (bad_signature): … forged or broken signature. Nice try.` Now change the `group` instead and run verify: `wrong_group`. Change `"seq":1` to `"seq":2` and get `bad_signature` (the seq is signed too).
2. **Booby-trap the key file.** Ask for a 1 TiB memory limit and watch it refused instantly, without the KDF running:
   ```
   python -c "import pathlib; p = pathlib.Path.home() / '.hearth/alice/identity.key'; d = bytearray(p.read_bytes()); d[33:41] = (2**40).to_bytes(8, 'big'); pathlib.Path('evil.key').write_bytes(d)"
   python -c "from hearth import keyfile, config; from pathlib import Path; keyfile.read_keyfile(Path('evil.key'), config.IDENTITY_TAG, 'x')"
   ```
   Expect `KeyFileError: key file asks for unsupported KDF limits (… memlimit=1099511627776)`. (Byte 33 = 13-byte tag + 16-byte salt + 4-byte opslimit.)
3. **Two processes, one folder.** In one terminal, hold the lock:
   `python -c "from hearth.lockfile import DataDirLock; from pathlib import Path; l = DataDirLock(Path.home() / '.hearth/alice'); l.acquire(); input('holding, press Enter')"`
   In another, run the `debug sign` command above: `error: … is already in use by another Hearth process`. Press Enter in the first terminal and try again: it works.

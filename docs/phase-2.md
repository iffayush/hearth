# Phase 2: Storage

> **Changed in Phase 4:** `debug sign` was replaced by `post`, and `debug log`/`debug verify` also work against a running node. See `phase-4.md`.

## What was built

Phase 2 gives each node a permanent memory: `hearth.db`, one SQLite file per data directory. Entries now survive restarts, and the store refuses anything that breaks a rule.

**The schema** (`store.py`, `SCHEMA`) is exactly the spec's: `meta`, `entries`, `members`, plus `chunks`, `outbox` and `invites`, which stay empty until Phases 5 and 6. `Store.open()` creates the tables on first use, writes `schema_version = 1`, and refuses a database with any other version (pre-MVP data is never migrated). It turns on WAL and foreign keys. There's one connection, used from one thread, and the module docstring explains why.

**Appending.** `Store.append_entry(raw)` is the only way in, and it's the function to read closely:

1. `parse_entry()` strictly parses the bytes.
2. `validate_contents()` (newly split out of `validate_entry()` in `entry.py`) checks everything except chain position: shape, size, group, signature, body, and whether the author is a member. Failures are `REJECTED`.
3. The entry is now known to be genuine. If we already hold that `seq`, `_compare_with_stored()` compares hashes: same hash means `DUPLICATE` (a silent no-op), a different one means `FORKED`.
4. Otherwise `check_chain()` runs. A `bad_prev` at exactly the next seq is also a fork; a gap is just `REJECTED`.
5. `_insert_entry()` writes the row inside a transaction. It sets `sort_ts = min(ts, now)` once and never again, stores the canonical re-encoding as `raw` (not the bytes as received), and applies any membership effect.

The order matters for security: the fork check only happens *after* the signature is verified, so a forged "Alice #2" can't get Alice marked as forked (`test_forged_conflict_cannot_frame_alice_as_forked`).

**Membership.** The admin's `group_create` puts the admin in `members`, and each `member_add` adds one more, both in the same transaction as the entry itself. `Store.create_group(raw)` sets `meta.group_id` and `meta.admin` from a validated `group_create` and stores it, all in one transaction.

**Forks** are recorded in `meta.forked_authors` (a JSON list); `debug log` prints a warning for each. Stopping sync with the sender comes in Phase 4.

**CLI.** `debug create-group` moved here from Phase 4 (your choice), so there's a group to post into. `debug sign "text"` now appends to your log, with no `--group` flag. `debug log` lists every entry. `debug verify` re-checks every stored signature, chain link and hash (`Store.verify_all()`). The Phase 1 file-based `debug verify FILE`, `--group` and `--out` are gone.

**Old data directories:** Phase 1 directories still work (they only hold `identity.key` and `config.json`), so there's nothing to delete.

## Try it yourself

```
python -m pytest
```
Expect `127 passed, 2 skipped`.

```
python -m hearth debug create-group --profile alice
python -m hearth debug sign "hello hearth" --profile alice
python -m hearth debug sign "second post" --profile alice
python -m hearth debug log --profile alice
```
`create-group` prints `Created group <64 hex>`. Each `sign` prints the entry and `stored as #2` / `#3`. `log` shows (without asking for a passphrase):
```
group 444d480d…  admin 56209da2…  members 1
56209da2  #1    group_create 2026-10-26 01:32:14  dd841dd38a9b  lit the hearth (created the group)
56209da2  #2    post         2026-10-26 01:32:15  c7cefa0c956d  'hello hearth'
56209da2  #3    post         2026-10-26 01:32:15  a55fc08a5967  'second post'
```
Close the terminal, open a new one and run `debug log` again: same output. Then `python -m hearth debug verify --profile alice` prints `All 3 entries verified: signatures good, chains unbroken.`

## Poke it

Run these from the folder that holds alice's data (`cd ~/.hearth`).

1. **Change one byte in a stored post.**
   `python -c "import sqlite3; db = sqlite3.connect('alice/hearth.db'); db.execute(\"UPDATE entries SET raw = replace(raw, 'hello', 'jello') WHERE seq = 2\"); db.commit()"`
   Run `debug log`: it shows `'jello hearth'`, because the database will happily store a lie. Now run `debug verify`: `BAD 56209da2 #2 (bad_signature) … Nice try.` with exit code 1. Change it back to `hello` and verify passes again.
2. **Fork yourself.** Copy the data dir (`cp -r alice alice-copy`, or copy the folder in Explorer), then `debug sign "from laptop A" --profile alice` and `debug sign "from laptop B" --profile alice-copy`. Hand the copy's #4 to the original:
   `python -c "from pathlib import Path; from hearth.store import Store; a = Store.open(Path('alice/hearth.db')); b = Store.open(Path('alice-copy/hearth.db')); print(a.append_entry(b.get_entry(b.admin(), 4).raw.encode()))"`
   You get `status='forked'`, and `debug log --profile alice` now starts with `WARNING: 56209da2 is forked`. (This snippet skips the lock; don't do that in real life.) Delete `alice-copy` afterwards.
3. **Bump the schema version** with `UPDATE meta SET value = '99' WHERE key = 'schema_version'` the same way, then run `debug log`: `error: database schema version is 99, this code needs 1 …`. Set it back to `'1'`.

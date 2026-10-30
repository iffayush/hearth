# Phase 4: Sync

> **Changed in Phase 5:** `debug create-group` became `create-group` (it also writes `group.key`), `debug join-group` now asks for the group key, `feed` asks for the passphrase when no node is running, and posts are encrypted. Delete Phase 4 data directories. See `phase-5.md`.

## What was built

Posts now travel. Alice writes a post, her node tells its peers, and bob's node fetches it, checks it and stores it. It works even if alice and bob are never online at the same time, as long as a third node relays.

**The conversation** (`sync.py`, class `Syncer`). When a connection becomes ACTIVE, `peer_active()` sends HEADS: for every author, our latest seq and its hash. On receiving HEADS, `_on_heads()` checks for forks (`_check_for_fork`: same seq, different hash). It then calls `_request_missing()`, which sends one WANT per author where the peer is ahead, admin first, because membership lives in the admin's log. The other side answers in `_on_want()`, where `_reply_budget()` fills ENTRIES up to 200 entries or 1.5 MiB. `_on_entries()` matches each reply to the oldest outstanding WANT. `_apply()` hands entries one by one to `store.append_entry()` and stops at the first bad one. Then the receiver announces fresh HEADS to every *other* peer, which is how the relay works. Finally it asks again for whatever is still missing.

**The pending queue** (`pending.py`). If bob's post arrives before the admin's `member_add` for bob, the store says "not a member". The post waits in `PendingQueue` and `_retry_pending()` tries again after every membership change. The queue holds at most 1,000 entries or 4 MiB, for at most 10 minutes. Entries that expire stop us asking that peer for that author.

**The control channel** (`control.py`). While `run` holds the data-dir lock, `post`, `feed` and the `debug` commands can't open the database. `cli.one_shot()` tries the lock. If the lock is held, `control.send_request()` sends the request over a token-protected local socket and `Node.handle_control()` runs it. Both paths end in the same place: `actions.perform()` in the new `actions.py`.

**CLI.** `__main__.py` now only parses arguments; the commands are in `cli.py`. There are new commands (`post`, `feed`, `debug join-group`, `debug add-member`), and `debug sign` is gone (use `post`).

**Old data directories:** nothing on disk changed, so Phase 2 and 3 folders still work. Posts are still plaintext (`TODO(phase 5)`).

## Try it yourself

```
python -m pytest
```
Expect `234 passed, 2 skipped`.

Set up a group (alice and bob from earlier phases; if alice already has a group from Phase 2, skip `create-group` and run `debug log --profile alice` to see its ID):
```
python -m hearth debug create-group --profile alice
python -m hearth debug add-member <bob's account ID> --profile alice
python -m hearth debug join-group <group ID> <alice's account ID> --profile bob
```
`create-group` prints the exact `join-group` line to copy. Then terminal 1, `python -m hearth run --profile alice --listen 127.0.0.1:7001`, and terminal 2, `python -m hearth run --profile bob --listen 127.0.0.1:7002 --dial 127.0.0.1:7001 --dial-pubkey <alice's ID>`. Bob logs `Stored 2 new entries from <alice8>` (the group's first two entries). In terminal 3:
```
python -m hearth post "the kettle is on" --profile alice
python -m hearth feed --profile bob
```
`post` prints `Posted as #3 …` without asking for a passphrase: alice's running node did the work. A second later bob's terminal logs `Stored 1 new entry`, and `feed` shows `[2026-10-26 19:04:17] <alice8>  the kettle is on`. Bob can reply with `post … --profile bob`.

## Poke it

1. **Catch up after being away.** Stop bob (Ctrl+C), post twice as alice, start bob again. He logs `Stored 2 new entries` within a second, because HEADS on connect show he's behind.
2. **Post before you're a member.** `init` a third profile, `carol`, and have her `debug join-group` without being added, then `post "hi" --profile carol`. You get `error: the store refused it: entry #1 by <carol8> is from someone who isn't a member of this group`. Even your own node won't let you write entries nobody else would accept.
3. **Fork yourself, for real this time.** Stop both nodes and copy alice's data folder to `alice-copy`. Post `"from A"` as alice and `"from B"` with `--data-dir <alice-copy>`. Start alice (7001) and bob (7002, dialling alice) as before, then start the copy with `run --data-dir <alice-copy> --dial 127.0.0.1:7002`. Bob logs `No longer syncing <alice8> with <alice8>: its HEADS hash for #4 differs from ours: <alice8> is forked`, and `debug log --profile bob` starts with `WARNING: <alice8> is forked`. Delete the copy afterwards.

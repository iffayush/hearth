# Hearth — P2P Forum Build Spec

Oct 25, 2026 · @Ayush

## Overview

Hearth is a private, invite-only forum whose data is replicated across its members' own devices: no company and no public website. Every member runs the same Python program (a "node"). One node runs nonstop on an on-prem VM: the home node. In practice this is hub-and-spoke: every member dials the home node, it is the group admin, it accepts invitations, and it holds a full copy of all posts and images. It is a mandatory, trusted hub; if it is down, members who can't reach each other can't sync and nobody new can join.

**MVP goals (all must work at the end of Phase 8):**

- An admin creates a group and becomes its first member.
- The admin invites a friend with a single link; the friend joins with no email, phone number or password.
- Members write text posts with optional images.
- Every member sees one merged feed in their browser at `http://127.0.0.1:8080`.
- Posts sync automatically between nodes, including when members are never online at the same time (via the home node).
- All content is end-to-end encrypted with a group key; a node without the key sees only ciphertext.
- Every post is signed; forged or tampered posts are rejected.

**Out of scope for the MVP** (designed for, not built): video, comments, editing or deleting posts, removing members and key rotation, direct member-to-member connections, Android, Tor, hosting websites, NAT hole punching, multiple groups per node, storage retention or cleanup.

**What "private" means in the MVP:** content is unreadable to anyone outside the group. It assumes every member is fully trusted: a member keeps the group key and every post and image they've received forever, and without removal and key rotation there is no way to cut someone off from future posts either. Tell the group this before inviting anyone.

**Guiding principle:** readability beats cleverness. The owner is an intermediate Python developer who wants to understand every line. Scale is 5 to 50 members, so performance is never a reason to make code harder to read.

## Instructions for Claude Code

Build one phase at a time, then stop and wait for the owner to say "continue". Never start the next phase on your own.

**At the end of every phase, deliver exactly this:**

1. The code for that phase, plus its tests, all passing (`python -m pytest`).
2. A file `docs/phase-N.md` (200 to 500 words) explaining in plain English what was built, how data flows through it, and which lines matter most, with file and function names.
3. A "try it yourself" section in that file: the exact terminal commands to see the phase working, and what output to expect.
4. Two or three "poke it" experiments, for example: "change one byte in a stored post and run verify; watch it fail".
5. A short summary in chat, then stop.

**Code style rules:**

- Python 3.12+, standard library first. Allowed third-party packages: `pynacl`, `aiohttp`, `pytest`, `pytest-asyncio`, plus dev tools `bandit` and `pip-audit`. Ask before adding anything else. Don't use `imghdr` (removed in 3.13).
- Small files, each with one job. Target 100 to 300 lines per file; split if a file passes 400.
- Functions short (under 40 lines where practical), named for what they do, with type hints everywhere.
- Every module starts with a docstring: what it does, how it fits in, and one joke (see Personality section).
- Comments explain why, not what. Any non-obvious line gets a comment.
- Plain data structures: `dataclasses` and dicts. No metaclasses, decorators beyond `@dataclass` and `@staticmethod`, or clever one-liners.
- No global mutable state except a single `Node` object passed explicitly.
- Every timeout, interval and rate limit is a constructor argument defaulting to a constant in `config.py` (for example `Peer(handshake_timeout=…, idle_timeout=…)`), so tests can pass 0.05 s. Never hard-code a duration inside logic.
- Use `logging`, never `print`, except in the CLI layer.
- Record every ambiguity you resolve in `docs/decisions.md`, one line each with the date and the phase.
- If a design decision in this spec turns out to be wrong or unclear, stop and ask instead of improvising.

**Correctness before decoration:** passing tests show the code behaves as intended in tested scenarios; they don't prove the protocol is secure. The owner reviews each phase before continuing. Personality touches (jokes, banners, Easter eggs) are never a reason to cut validation, sync correctness or failure handling, and a phase never waits on them.

## Tech stack and project layout

The stack is Python plus two libraries; everything else is standard library.

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12+ | Readable; owner is intermediate |
| Crypto | `pynacl` (libsodium) | Audited, one call per operation, hard to misuse |
| Networking | `asyncio` TCP streams | Built in; every message is inspectable |
| Local web UI | `aiohttp` | Small, async, works with the same event loop |
| Storage | `sqlite3` + a chunk folder | Built in; one file per node |
| Encoding | `json` (wire) + canonical JSON (signed bytes) | Human-readable during debugging |
| Hashing | `hashlib.blake2b` (32-byte digest) | Built in, fast, matches libsodium |
| Tests | `pytest`, `pytest-asyncio` | Standard |
| Frontend | One `index.html`, vanilla JS, plain CSS | No build tools, nothing hidden |

**Project layout:**

```
hearth/
  pyproject.toml
  README.md
  .github/workflows/ci.yml   # pytest, bandit, pip-audit on every push
  hearth/
    __init__.py
    __main__.py      # CLI entry: python -m hearth <command>
    config.py        # compiled-in limits, defaults, timeouts
    settings.py      # load/save per-node config.json
    canonical.py     # strict JSON parsing + deterministic bytes
    keyfile.py       # passphrase-encrypted key files (format below)
    identity.py      # signing + encryption key pairs: create, load, save
    entry.py         # Entry dataclass, sign, pure validate_entry()
    crypto.py        # group key, encrypt/decrypt content and chunks
    chunks.py        # magic-byte checks, split, reassemble, verify
    store.py         # SQLite schema and all queries
    group.py         # group state, membership, admin rules
    protocol.py      # message types, framing, schemas, limits
    peer.py          # one connection: handshake, session, state machine
    sync.py          # heads / want / entries, pending queue
    fetcher.py       # ChunkFetcher: who to ask, coalesced futures
    outbox.py        # posts waiting for their chunks to be acked
    invite.py        # create and redeem invite links
    feed.py          # decrypt, clamp, sort, paginate, in-memory cache
    lockfile.py      # one process per data dir
    control.py       # local control channel: CLI commands talk to a running node
    node.py          # Node object: wires everything, runs the loop
    web.py           # local HTTP API for the UI
    static/
      index.html
      app.js
      style.css
  tests/
    conftest.py          # make_node(), eventually(), seed_group()
    test_<module>.py     # one file per module
    test_cli.py          # subprocess smoke tests
    test_three_nodes.py  # end-to-end convergence test
  docs/
    decisions.md
    phase-1.md ... phase-8.md
    deploy.md
  deploy/
    hearth-home.service  # systemd unit for the home node
```

**Data directory:** each node keeps everything under `~/.hearth/<profile>/` (override with `--data-dir`), so several nodes can run on one laptop for testing: `identity.key`, `hearth.db`, `chunks/`, `config.json`.

**`config.py` vs `config.json`:** `config.py` holds compiled-in limits, defaults and timeouts that are the same for everyone. `config.json` holds per-node runtime settings: `profile`, `listen` address, `role` (`member` or `home`), the pinned home node `host` and `pubkey` (from the invite link), `web_port`, and on the home node `storage_quota_gib`. The group ID and admin pubkey live in the database's `meta` table, not in `config.json`.

## Architecture and data model

The forum is a set of per-member append-only logs; each node merges them into a feed locally. There is no shared database and no server-side logic. The code is layered so each layer only calls the one below: web UI → node → sync → peer session → TCP, with store, log, crypto and identity used across them.

&#91;embedded content: node layers · member laptop and home node\]

Both sides run identical code; the home node only turns off the UI, acts as admin, and keeps a copy of every chunk.

### Identity

- On first run, generate two independent key pairs (following libsodium's guidance to keep signing and encryption keys separate): an Ed25519 `SigningKey` and an X25519 `PrivateKey`. The hex-encoded Ed25519 public key is the member's account ID. The X25519 key is used only to receive the sealed group key when joining. No Ed25519-to-Curve25519 conversion anywhere.
- The X25519 public key is never published on its own; it travels inside `INVITE_REDEEM`, which is sent within the session authenticated by the Ed25519 handshake, so the home node knows it belongs to that identity.
- **Key file format** (`keyfile.py`, used for both `identity.key` and `group.key`): `tag` + `salt` (16 bytes) + `opslimit` (4-byte big-endian) + `memlimit` (8-byte big-endian) + `SecretBox` ciphertext. Tags are `b"hearth-id-v1\n"` and `b"hearth-group-v1\n"`; loading checks the tag so the files can't be swapped. `identity.key`'s plaintext is a small canonical JSON object holding both private keys. The key is `nacl.pwhash.argon2id.kdf` over the passphrase with the stored salt and limits. Files are written atomically (temp file + `os.replace`) with mode `0600`.
- **KDF parameter bounds:** the stored limits drive an expensive operation, so loading accepts exactly two `(opslimit, memlimit)` pairs, libsodium's `INTERACTIVE` and `MODERATE` constants, and rejects anything else before calling the KDF. Files larger than 4 KiB are rejected unread.
- **Passphrase:** both files use the same passphrase. Every command that opens keys (`init`, `join`, `run`, `invite`, `create-group`, `debug …`) prompts with `getpass` (twice when creating), or reads `--passphrase-file`. A passphrase file must be mode `0600` on POSIX or the command refuses to run. Changing the passphrase (`rekey`) is out of scope for the MVP.
- **Argon2id limits:** default `MODERATE` (256 MiB). The home node may use `INTERACTIVE` (64 MiB) via `--kdf interactive` if Phase 8 benchmarking shows MODERATE is too heavy for the VM; the choice is stored in the file header and justified in `docs/phase-8.md`.
- `Identity.create(passphrase)` and `Identity.load(path, passphrase)` take the passphrase as an argument, so tests never shell out or prompt.

### Log entry

Every action is an entry in its author's log. Fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `v` | int | Format version, always `1` |
| `group` | hex str | Group ID |
| `author` | hex str | Author's public key |
| `seq` | int | 1, 2, 3 … with no gaps |
| `prev` | hex str or null | Hash of the author's previous entry; null when `seq` is 1 |
| `ts` | int | Author's clock, Unix milliseconds; for display and ordering only, never trusted for security |
| `kind` | str | `group_create`, `member_add`, `profile`, `post` |
| `body` | dict | Kind-specific content (below) |
| `sig` | hex str | Ed25519 signature |

- **Signed bytes** = `b"hearth/entry/v1\n"` + canonical JSON of every field except `sig`.
- **Canonical JSON** (`canonical.py`): sorted keys, separators `(",", ":")`, UTF-8, `ensure_ascii=False`. Parsing is strict and rejects: floats, `NaN` and `Infinity` (via `parse_constant`), duplicate keys (via `object_pairs_hook`), booleans where an int is expected (check `type(x) is int`, never `isinstance`), and lone surrogates (catch the encode error and return a clear reason).
- **Entry hash** = BLAKE2b-256 of the same signed bytes. `prev` links to it, forming a hash chain per author.
- **Validation is a pure function:** `validate_entry(entry, head, members, admin, group_id) -> Reason | None` in `entry.py`, with no database access. `head` is the author's current stored head as a `Head(seq, hash)` dataclass, or `None` if we have nothing from that author. Rules: if `head` is `None`, the entry must have `seq == 1` and `prev is None`; otherwise `seq == head.seq + 1` and `prev == head.hash`. Other checks: `group` equals this node's group, signature verifies, `kind` is known, `body` passes its schema, the author is a member (except the admin's `group_create`), raw entry under 64 KiB. The store looks up `head` and the member set, calls the validator, then writes.
- **One writer per identity (hard assumption):** an identity's keys must only ever be used from one data directory. The lockfile enforces one process per directory but can't stop someone copying a key to a second directory. If that happens, the two copies can produce divergent chains. Hearth detects this but doesn't repair it: a second, different entry for an `author` + `seq` we already hold is a fork. Reject it, mark that author as forked in `meta`, stop syncing that author with the peer that sent it, and show a warning in `status` and the UI. Receiving the identical entry twice is a silent no-op.
- **Timestamps are untrusted:** each node computes `sort_ts = min(ts, received_at)` once, when it first stores an entry, and keeps it. Feed order, pagination and display all use `sort_ts`, so the ordering key never changes between requests and a future-dated post shows at the moment it arrived. Different nodes may order near-simultaneous posts slightly differently; that's accepted. A member can still backdate a post (accepted residual).

### Entry bodies

- `group_create` (admin, seq 1): `{"admin": <pubkey>}`. The group ID is a random 32-byte value created with it.
- `member_add` (admin only in the MVP): `{"member": <pubkey>}`. The membership list is the admin's chain of these.
- `profile`: `{"box": <encrypted {"name": str}>}`; the latest one wins.
- `post`: `{"box": <encrypted post>}`. Plaintext before encryption: `{"text": str, "images": [manifest, …]}`, text up to 10,000 characters, up to 4 images.

### Group encryption

- One 32-byte random group key, created with the group. Every `box` is `SecretBox(group_key).encrypt(plaintext)` (XSalsa20-Poly1305 with a random 24-byte nonce), base64-encoded.
- Stored in `group.key` using the key file format above, with the `hearth-group-v1` tag and the same passphrase as `identity.key`.
- Only entry metadata (author, seq, kind, timestamp) is visible without the key; content never is.

### Images and chunks

- Accept JPEG, PNG, WebP, GIF only, detected by explicit magic-byte checks written in `chunks.py` (not `imghdr`, not the file name). Maximum 10 MiB per image.
- Split the image into 1 MiB pieces; encrypt each with the group key; chunk ID = BLAKE2b-256 of the ciphertext. Save as `chunks/<first 2 hex>/<chunk id>`. Nodes never delete chunks they created.
- Manifest (inside the encrypted post): `{"mime", "size", "sha": <BLAKE2b of the whole plaintext image>, "chunks": [chunk ids in order]}`.
- On fetch, verify each chunk's hash before saving, and the whole-image hash after reassembly.
- **Delivery guarantee:** a post with images first goes to the `outbox` table. The node pushes its chunks to the home node (`CHUNK_PUSH`) and appends the post entry to its log only after the home node has acknowledged every chunk (`CHUNK_ACK`). Until then, the UI shows the post as "waiting to upload". So any post that exists in a log has its chunks on the home node. Text-only posts skip the outbox. This is the only mechanism that puts chunks on the home node; it never fetches chunks on its own.
- **Storage quota:** the home node refuses `CHUNK_PUSH` with `ERROR quota_exceeded` once `chunks/` reaches `storage_quota_gib` (default 20 GiB). The post stays in the poster's outbox and the UI says why. `status` reports usage against the quota. There's no retention or cleanup in the MVP; the quota is the only control on growth.
- **Fetching (`fetcher.py`):** a `ChunkFetcher` on member nodes keeps one future per chunk ID, so concurrent requests for the same chunk share one transfer. It asks the home node, with a 10-second timeout and up to 3 attempts. Member nodes fetch only when the UI asks for an image.

### Storage schema (SQLite)

```sql
-- meta keys: schema_version, group_id, admin, forked_authors (JSON list)
CREATE TABLE meta    (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entries (
  author TEXT NOT NULL, seq INTEGER NOT NULL,
  hash TEXT NOT NULL UNIQUE, prev TEXT, ts INTEGER NOT NULL,
  sort_ts INTEGER NOT NULL,                -- min(ts, received_at), set once
  kind TEXT NOT NULL, raw TEXT NOT NULL,   -- raw = canonical JSON
  PRIMARY KEY (author, seq));
CREATE INDEX entries_feed ON entries (kind, sort_ts DESC, author, seq);
CREATE TABLE members (pubkey TEXT PRIMARY KEY, added_seq INTEGER NOT NULL);
CREATE TABLE chunks  (chunk_id TEXT PRIMARY KEY, size INTEGER NOT NULL);
CREATE TABLE outbox  (id INTEGER PRIMARY KEY, created INTEGER NOT NULL,
                      plaintext_box TEXT NOT NULL, chunk_ids TEXT NOT NULL,
                      acked_ids TEXT NOT NULL DEFAULT '[]');
CREATE TABLE invites (token_hash TEXT PRIMARY KEY, created INTEGER NOT NULL,
                      expires INTEGER NOT NULL, used_by TEXT,
                      sealed_key TEXT);       -- kept so redemption is retryable
```

Member nodes store the home node's hostname, port and pubkey in `config.json`; there is no peers table in the MVP.

- **One connection, one thread:** the `Node` owns a single `sqlite3` connection; all access happens on the event-loop thread, with no thread pool and no `to_thread`. Queries are tiny at this scale. Document this in `store.py`'s docstring.
- Always use parameterised queries. Enable `PRAGMA foreign_keys=ON` and `PRAGMA journal_mode=WAL`.
- `raw` holds the canonical JSON of the entry (re-encoded after strict parsing, never the bytes as received), so signatures can be re-verified at any time and relayed entries are byte-identical everywhere.
- On append, check `entry["group"] == meta.group_id`.
- `outbox.plaintext_box` is already encrypted with the group key; nothing readable is ever stored.
- **One process per data dir:** `lockfile.py` takes an exclusive lock on `<data-dir>/hearth.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) at startup. Only the process holding the lock ever opens the database or the key files.
- **Control channel (`control.py`):** `run` opens a local control channel. One-shot commands (`post`, `feed`, `status`, `invite`, `backup`, `debug add-member`, and so on) first try the lock: if it's free, they open the data dir directly as before; if it's held, they send their request over the control channel instead, and the running node does the work. This keeps a single writer per log, lets `status` report live connection state, and avoids a second Argon2id derivation. On POSIX the channel is a Unix socket at `<data-dir>/control.sock` (the directory is `0600`-protected, and systemd allows `AF_UNIX`). On Windows it's a TCP port on `127.0.0.1` with a random 32-byte token written to `<data-dir>/control.token` (mode `0600`); each request carries the token, compared in constant time. Requests and replies are one JSON line each, validated like network messages.

## Wire protocol

Nodes talk over TCP with length-prefixed JSON frames; after a two-message handshake, every frame is encrypted with per-direction session keys.

### Framing

- Each frame = 4-byte big-endian length + payload. Reject any frame over 2 MiB and close the connection.
- Before the session is established (`HELLO`, `AUTH`), the payload is UTF-8 JSON.
- After, the payload is `SecretBox(direction_key).encrypt(plaintext, nonce).ciphertext`, where the nonce is a per-direction frame counter `n` (0, 1, 2 …) encoded as 24 bytes big-endian. The nonce is not sent; both sides track it. Each direction has its own key, so a counter nonce is never reused.
- **Counter rule:** the receiver increments its expected `n` for every frame it receives, before any decryption or validation. A frame that fails to decrypt (tampered, replayed, reordered) closes the connection. A frame that decrypts but contains malformed or unknown JSON is logged and dropped, and the counter has already advanced, so the next frame is still accepted.
- Every JSON message has a `"t"` (type) field.

### Handshake

1. Both sides send `HELLO`: `{"t": "HELLO", "proto": "hearth/1", "pubkey": <Ed25519 hex>, "eph": <fresh X25519 public key hex>, "nonce": <32 random bytes hex>}`.
2. Both sides send `AUTH`: `{"t": "AUTH", "sig": <Ed25519 signature>}` over `b"hearth/handshake/v1\n"` + the initiator's then responder's `pubkey`, `eph` and `nonce`, concatenated as hex strings. This concatenation is unambiguous only because all six values are exactly 64 hex characters; validate that length and say so in a comment so nobody refactors it into a variable-length join. Verify the other side's signature against its `pubkey`.
3. Derive session keys from the ephemeral keys. Initiator: `rx, tx = crypto_kx_client_session_keys(own_eph_pk, own_eph_sk, peer_eph_pk)`. Responder: `rx, tx = crypto_kx_server_session_keys(own_eph_pk, own_eph_sk, peer_eph_pk)`. Both send with `tx` and receive with `rx` (the initiator's `tx` equals the responder's `rx`). Write this exact mapping in `peer.py`'s docstring and test it. Discard ephemeral private keys afterwards (forward secrecy).
4. A member node always dials the home node with its pubkey pinned (from `config.json` or the invite link). If the responder's pubkey doesn't match, abort.
5. Handshake timeout: `config.HANDSHAKE_TIMEOUT` (10 s), injectable.

### Access control after the handshake

Two separate questions, never mixed up in code:

- **Identity authentication** (who is this?): answered by the handshake signature, plus the pinned pubkey when dialing the home node.
- **Group authorization** (what may they do?): answered by `Peer`'s `authorizer: Callable[[str], bool]`, which in Phase 6 returns true for the admin and any member in the local membership table. Phase 3 passes `lambda _: True`.

A joining node has no membership data yet, so it cannot use the authorizer for the home node. Instead it uses the dedicated `JOINING` state below, where the only thing it trusts is the pinned key from the invite link. Once `INVITE_OK` has set `meta.admin`, the authorizer returns true for the home node because it is the admin.

- On the **home node**, an authorized peer goes to `ACTIVE`; an unknown peer goes to `LIMITED`, where nothing is sent to it unprompted (no `HEADS`) and the only accepted message is `INVITE_REDEEM`.
- On a **member node**, the home node is authorized as admin, so the session goes straight to `ACTIVE`.

**Session state machine, both sides** (also written as a table in `docs/phase-3.md` and extended in `docs/phase-6.md`). Messages on one TCP connection arrive in order, so the joiner's first `HEADS` always reaches the home node after it has switched to `ACTIVE`.

| Side | State | Event | Next state | Action |
| --- | --- | --- | --- | --- |
| both | `AWAIT_HELLO` | valid `HELLO` | `AWAIT_AUTH` | send own `AUTH` |
| home | `AWAIT_AUTH` | valid `AUTH`, peer is admin or member | `ACTIVE` | derive keys, send `HEADS` |
| home | `AWAIT_AUTH` | valid `AUTH`, peer unknown | `LIMITED` | derive keys, send nothing |
| home | `LIMITED` | `INVITE_REDEEM`, token valid (or same-pubkey retry) | `ACTIVE` | commit `member_add`, send `INVITE_OK`, then `HEADS` |
| home | `LIMITED` | `INVITE_REDEEM`, token invalid | `CLOSED` | send `ERROR bad_invite` |
| home | `LIMITED` | any other message | `CLOSED` | send `ERROR not_member` |
| member | `AWAIT_AUTH` | valid `AUTH`, pinned key matches | `ACTIVE` | derive keys, send `HEADS` |
| joiner | `AWAIT_AUTH` | valid `AUTH`, key matches invite `k` | `JOINING` | derive keys, send `INVITE_REDEEM`, nothing else |
| joiner | `JOINING` | `INVITE_OK` with `group == g`, `admin == k`, key unseals | `ACTIVE` | save group key and `meta`, send `HEADS` (empty), start syncing |
| joiner | `JOINING` | `INVITE_OK` that fails any check | `CLOSED` | abort join, save nothing |
| joiner | `JOINING` | `ERROR`, any other message, or 15 s timeout | `CLOSED` | report; the user may retry the same link |
| any | any | pinned key mismatch, timeout, bad frame or signature | `CLOSED` | log reason |

### Message types

| Type | Payload | Purpose |
| --- | --- | --- |
| `HEADS` | `{"heads": {author: [latest_seq, latest_hash]}}` | "Here is how far I have every log, and the hash at that point." Sent on entering `ACTIVE`, after each new local entry, and every 30 s. The hash lets peers detect forks. |
| `WANT` | `{"author", "from", "to"}` | Request one author's entries. `from` and `to` are inclusive; `from` must be our current head for that author + 1; `to - from < 200`. |
| `ENTRIES` | `{"entries": [raw, …]}` | Reply to `WANT`, in `seq` order, starting at `from`. Capped at 200 entries and 1.5 MiB of raw JSON, whichever comes first, so it always fits in a frame. A reply may cover only part of the range; the receiver re-requests the rest. Unsolicited `ENTRIES` are dropped. |
| `CHUNK_WANT` | `{"ids": [chunk id, …]}` | A member asks the home node for up to 4 chunks. |
| `CHUNK` | `{"id", "data": base64}` | One chunk. The receiver checks the hash matches `id`. |
| `CHUNK_MISSING` | `{"ids": […]}` | "I don't have these." |
| `CHUNK_PUSH` | `{"id", "data": base64}` | A poster uploads one of its own chunks to the home node. Only the home node accepts it, only from members. |
| `CHUNK_ACK` | `{"id"}` | Home node confirms a pushed chunk is verified and stored. |
| `INVITE_REDEEM` | `{"token": base64url, "enc_pubkey": <X25519 hex>}` | A joiner redeems an invite and says where to seal the group key. Only valid in `LIMITED`. |
| `INVITE_OK` | `{"group", "admin", "sealed_key": base64}` | Group key sealed to `enc_pubkey` with `SealedBox`. |
| `PING` / `PONG` | `{}` | Keepalive every 20 s; drop the peer after 60 s of silence (both injectable). |
| `ERROR` | `{"code", "msg"}` | Then close. Codes: `bad_frame`, `not_member`, `bad_invite`, `rate_limited`, `quota_exceeded`, `internal`. |

Unknown message types or extra fields: log and drop the message; don't crash.

### Sync algorithm

1. On entering `ACTIVE`, both sides send `HEADS`.
2. For each author in the peer's `HEADS`: if the peer's seq is higher than ours, send `WANT` for the gap, one range at a time per author. If the peer's seq is equal to or lower than ours, compare its hash with our stored hash at that seq; a mismatch means a fork (see "One writer per identity").
3. Apply received entries in order; stop at the first invalid entry from that author and log why. An entry whose `prev` doesn't match our head is also treated as a fork.
4. After applying new entries, send our updated `HEADS` to all other `ACTIVE` peers. Through the home node, this is how content reaches friends who are never online together.
5. Membership entries are applied before posts: if a post's author isn't a member yet, keep the entry in a pending queue and retry after the next membership update. The queue is capped at 1,000 entries and 4 MiB total; entries older than 10 minutes are evicted, and their author isn't re-requested from that peer for the rest of the session.
6. Duplicate connections from the same peer are allowed; appends are idempotent (identical entry = no-op) and the pending queue ignores entries it already holds.

### Topology and connections

- The home node listens on `0.0.0.0:7777`. Member nodes don't listen on the network at all in the MVP; they only dial the home node. Direct member-to-member connections (and their trust-on-first-use pinning) are out of scope.
- IPv4 only for the MVP (recorded in `docs/decisions.md`). IPv6 is a later change to the `--listen` handling.
- The home node's address is stored by hostname and port exactly as given in the invite link (`h=`) and resolved at every dial, so a dynamic-DNS change doesn't strand anyone.
- Reconnect with exponential backoff: 1 s, 2 s, 4 s … capped at 60 s, with ±20% jitter.
- For tests on one laptop, `--dial host:port` and `--listen host:port` let any profile play either role.

### Invites

The home node is the group admin, because it's always online to accept new members.

1. The owner runs `python -m hearth invite --expires 48h` on the home node (over SSH). It creates a random 32-byte token, stores only its BLAKE2b hash with an expiry, and prints a link: `hearth://join?h=<hostname>:<port>&k=<home pubkey>&g=<group id>&t=<token>`.
2. The friend runs `python -m hearth join "<link>"`. Their node prompts for a new passphrase (or reads `--passphrase-file`), creates an identity (both key pairs), dials the home node, checks the home node's pubkey matches `k`, enters `JOINING`, and sends `INVITE_REDEEM` with the token and its X25519 public key.
3. The home node, in one SQLite transaction: checks the token hash exists and is unexpired; runs `UPDATE invites SET used_by = ? WHERE token_hash = ? AND used_by IS NULL` and checks one row changed; appends the `member_add` entry; seals the group key to the joiner's `enc_pubkey` and stores it in `invites.sealed_key`. Then it replies `INVITE_OK`. If the process dies mid-way, the whole transaction rolls back and the token stays usable.
4. **Retry is idempotent:** if the same token is presented again by the same Ed25519 pubkey that used it (for example because `INVITE_OK` was lost), the home node returns the stored `INVITE_OK` again. A different pubkey gets `ERROR bad_invite`.
5. The joiner checks `INVITE_OK.group == g` and `INVITE_OK.admin == k` and that the key unseals, before saving anything; a failure aborts the join.
6. The joiner saves the group key and `meta`, then syncs. It writes its `profile` entry only after it has received the admin's log up to and including its own `member_add`, so validation stays uniform with no special case for self-authored entries.
7. Wrong or expired tokens get `ERROR bad_invite`. Limit redemption attempts to 5 per IP address per 10 minutes.

## Security requirements

These rules are not optional; each one gets at least one test.

**Threat model (MVP):** protect content from anyone outside the group who watches the network or relays traffic. Detect and reject forged or altered posts. At rest, protection depends on what an attacker obtains:

- **A member laptop's data directory or disk, without the passphrase:** keys and content stay protected (the passphrase is typed at startup and never stored).
- **A copy of the home node's data directory or backups, without `/etc/hearth/passphrase`:** keys and content stay protected. Backups must never include the passphrase file.
- **A full image of the home node VM's disk:** this includes the passphrase file, so it is equivalent to a compromised home node: the attacker can decrypt everything. Encrypting the VM's disk at the hypervisor level narrows this, but the MVP doesn't claim protection here.

Out of scope: a malicious member (they hold the group key), a compromised or fully imaged home node, hiding IP addresses (no Tor yet). The protocol is custom and unaudited: it uses established libsodium primitives, but the composition hasn't been independently reviewed, and green tests don't prove it secure.

**Accepted leaks, stated so nobody assumes otherwise:**

- **Metadata at rest:** someone who copies a node's disk learns the social graph and activity pattern without any passphrase: author pubkeys, membership, entry kinds, timestamps, sizes, and the home node's hostname. Only content and keys are encrypted.
- **Traffic analysis:** `HELLO` and `AUTH` are plaintext and carry pubkeys, so a network observer (including the fallback VPS, if used) learns who connects to the home node and when, and roughly how much they transfer. Everything after the handshake is encrypted.
- **Backdated posts:** a member can set a timestamp in the past; future timestamps are clamped to arrival time.
- **Forks:** if one identity's keys are used from two directories, nodes detect the fork and stop syncing that author, but don't converge automatically.

**Cryptography**

- Use only PyNaCl primitives. Never implement or combine raw ciphers by hand.
- All randomness in crypto and security paths (keys, nonces, tokens, salts) comes from `nacl.utils.random` or `secrets`. The stdlib `random` module is fine for non-security things like the CLI banner tagline and backoff jitter.
- Compare secrets and hashes with `hmac.compare_digest` or `nacl.bindings.sodium_memcmp`.
- Every signature uses a domain-separation prefix (`hearth/entry/v1`, `hearth/handshake/v1`) so a signature from one context can't be replayed in another.

**Input handling**

- Treat every byte from the network as hostile. Validate each message against an explicit schema in `protocol.py` (types, required fields, lengths, hex formats) before use.
- Enforce limits from `config.py`: frame 2 MiB, `ENTRIES` reply 1.5 MiB, entry 64 KiB, post text 10,000 characters, 4 images per post, image 10 MiB, `WANT` range 200, `CHUNK_WANT` 4, pending queue 1,000 entries and 4 MiB.
- Parse JSON only through `canonical.py`'s strict parser (no floats, `NaN`, duplicate keys, bool-as-int or lone surrogates). Never `pickle`, `eval`, `exec` or `yaml.load`.
- Chunk file paths come only from validated 64-character lowercase hex IDs, never from user or network strings. Resolve and confirm every path stays inside `chunks/`.
- Web API path parameters are validated before use: author = 64 lowercase hex, `seq` a positive int below 2^31, image index 0 to 3.
- SQL only through parameterised queries.

**Secrets at rest**

- `identity.key` and `group.key` use the tagged key file format with an Argon2id-derived key; only the `INTERACTIVE` and `MODERATE` parameter pairs are accepted when loading. Files created with mode `0600` (on Windows, in the user profile directory). Passphrase files must be `0600` or the command refuses to run.
- Never log keys, tokens, passphrases, plaintext post content or decrypted images. Log pubkeys shortened to 8 hex characters.
- Invite tokens are stored only as hashes, single-use per pubkey, and expire (default 48 hours).

**Local web UI**

- Bind `aiohttp` to `127.0.0.1` only; refuse to start if configured otherwise.
- Require a random per-launch session token: the CLI prints `http://127.0.0.1:8080/?token=…`; the page sends it in an `X-Hearth-Token` header on every API call. This stops other websites in the browser from calling the API. No cookies.
- **Images:** because `<img src>` can't send a header, `app.js` fetches each image with `fetch()` plus the token header, turns the response into a `Blob`, and sets `img.src = URL.createObjectURL(blob)`. It revokes old object URLs (`URL.revokeObjectURL`) whenever the feed re-renders, to avoid memory leaks. This is why the CSP allows `blob:` in `img-src`.
- Check the `Host` header is `127.0.0.1:<port>` or `localhost:<port>` to block DNS rebinding.
- Send `Content-Security-Policy: default-src 'self'; img-src 'self' blob:; object-src 'none'; frame-ancestors 'none'`, plus `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.
- Render all post text with `textContent`, never `innerHTML`. Serve images with their verified MIME type.
- Cap upload size at the server with `client_max_size = 45 MiB` (4 × 10 MiB images plus text and overhead), as well as in the browser.

**Network and availability**

- Handshake timeout 10 s; idle timeout 60 s; at most 64 simultaneous connections on the home node and 8 per IP address.
- Rate-limit invite redemption (5 per IP per 10 minutes) and `WANT`/`CHUNK_WANT` requests (100 per minute per peer).
- One misbehaving peer must never crash the node: catch errors per connection, log, and close that connection only.

**Dependencies and code hygiene**

- Pin dependency versions in `pyproject.toml`.
- CI (GitHub Actions) runs `pytest`, `bandit -r hearth` and `pip-audit` on every push; a phase isn't finished until CI is green or every finding is justified in the phase doc.
- No hard-coded secrets, IPs or passwords in code; configuration lives in `config.json` and CLI flags.

## Build phases

Eight phases, each ending in something the owner can run and see. Stop after every phase (see Instructions). Data formats change between some phases (for example the post body in Phase 5), so each phase doc says whether old test data directories must be deleted; no migration code is written for pre-MVP data.

### Phase 1: Identity and signed entries

- **Build:** `config.py`, `settings.py`, `canonical.py`, `keyfile.py`, `identity.py`, `entry.py`, `lockfile.py`, `__main__.py` with `init` and a debug command `debug sign "text" --group <hex>` (there's no group yet, so the group ID is passed explicitly; don't invent a placeholder). Set up `tests/conftest.py`, `.github/workflows/ci.yml` and `docs/decisions.md`.
- **Details:** generate both key pairs, encrypt-at-rest in the tagged format with bounded KDF parameters, save and load an identity; strict canonical JSON; create `Entry` objects with seq, prev, hash and signature; pure `validate_entry(entry, head, …)` returns a clear reason on failure.
- **Done when:** `python -m hearth init --profile alice` creates an encrypted key file; `debug sign` prints a signed entry; tests show that changing any byte, a wrong seq (against a given `head`), a first entry that isn't seq 1, or a broken `prev` fails verification with a specific reason; canonical JSON rejects floats, `NaN`, duplicate keys, `true` as an int and lone surrogates; a wrong passphrase, a swapped key file, and a key file with out-of-range KDF parameters fail cleanly (the last without running the KDF); a second process on the same data dir is refused; `test_cli.py` smoke-tests `init` and `debug sign` via subprocess with `--passphrase-file`; CI is green.

### Phase 2: Storage

- **Build:** `store.py` with schema creation, `schema_version` in `meta`, and functions: `append_entry`, `get_entry`, `get_range`, `head(author)`, `heads`, `members`, `add_member`.
- **Details:** `append_entry` looks up the author's `Head(seq, hash)` and the member set, calls the pure `validate_entry`, then writes in a transaction, setting `sort_ts = min(ts, now)` once. It stores the canonical re-encoding as `raw`; identical duplicates are no-ops; a conflicting entry marks the author as forked. One connection on the event-loop thread.
- **Done when:** entries survive a restart; `python -m hearth debug log --profile alice` lists stored entries; tests cover forks (and the forked flag), gaps, identical duplicates, a wrong group ID, and that `sort_ts` doesn't change once stored.

### Phase 3: Two nodes talk

- **Build:** `protocol.py` (framing, schemas, limits), `peer.py` (handshake, counter-nonce session, state machine, `authorizer` argument, injectable timeouts), a minimal `node.py` that listens and dials.
- **Details:** this phase passes `authorizer=lambda _: True`. Write the state machine table into `docs/phase-3.md`.
- **Done when:** two terminals (`run --profile alice --listen 127.0.0.1:7001` and `run --profile bob --listen 127.0.0.1:7002 --dial 127.0.0.1:7001`) complete the handshake and exchange `PING`/`PONG`; logs show the shortened pubkeys. Tests (using 0.05 s timeouts): the `crypto_kx` key mapping, tampered frame, oversized frame, replayed frame, malformed JSON that's dropped without breaking the next frame, wrong pinned pubkey, handshake timeout — each behaves as specified without crashing the node.

### Phase 4: Sync

- **Build:** `sync.py` (`HEADS`, `WANT`, `ENTRIES` with the byte budget, partial-range handling, re-broadcast, pending queue), `control.py`, CLI `post "text"` (plaintext body for now, marked `TODO(phase 5)`) and `feed`, and two membership debug commands: `debug create-group` (run on the admin profile; writes `group_create` and sets `meta.group_id` and `meta.admin`) and `debug add-member <pubkey>` (admin writes `member_add`). These bodies are plaintext pubkeys, so they need no crypto yet. Add a `seed_group(admin_node, *member_nodes)` helper to `tests/conftest.py`.
- **Details:** without these, every post fails validation because nobody is in a group. Members learn the group ID and admin from a `debug join-group <group id> <admin pubkey>` command that only sets `meta`; Phase 6 replaces all three with real invites.
- **Done when:** with alice as admin and bob added as a member, a post written with `post --profile alice` (sent to alice's running node over the control channel) appears in bob's `feed` within a few seconds. `tests/test_three_nodes.py`: A posts while C is offline; B syncs from A; A goes offline; C comes online, syncs from B and gets A's post. All three end with identical heads. Extra tests: an author with entries totalling more than 2 MiB syncs across several `ENTRIES` frames; unsolicited `ENTRIES` are ignored; two connections between the same nodes don't duplicate anything; a one-shot command against a running node goes through the control channel and never opens the database; a control request with a wrong token (Windows path) is refused.

### Phase 5: Group encryption and images

- **Build:** `crypto.py`, `chunks.py`, `group.py` (group key file), `fetcher.py`, `outbox.py`; `CHUNK_WANT`, `CHUNK`, `CHUNK_MISSING`, `CHUNK_PUSH`, `CHUNK_ACK` in `sync.py`; the storage quota; CLI `create-group` (Phase 4's `debug create-group` plus generating the group key file), `post "text" --image cat.jpg`, `debug image <author> <seq> <index> --out file`. `seed_group` in `conftest.py` now also copies the group key to test members.
- **Details:** posts now carry only `box` (delete Phase 4 data dirs). A post with images waits in the outbox until the home-role profile acks every chunk, then the entry is appended. Member nodes fetch chunks from the home node on demand through `ChunkFetcher`.
- **Done when:** opening `hearth.db` in any SQLite viewer shows no readable post text; chunk files are random bytes; bob reassembles alice's image byte-for-byte. Availability test: alice posts an image and goes offline immediately after the post appears; bob still gets the image from the home node. Other tests: wrong group key fails to decrypt, a corrupted chunk is rejected by hash, a fake image (wrong magic bytes) is refused, two simultaneous requests for one chunk cause one transfer, a push over the quota gets `quota_exceeded` and the post stays in the outbox.

### Phase 6: Membership and invites

- **Build:** `invite.py`; membership in `group.py` and `store.py`; switch `Peer` to the membership authorizer; the `LIMITED` and `JOINING` states; CLI `invite`, `join` and `status` (member count, connected peers, entries per author, forked authors, disk usage against quota). Write the full two-sided state table into `docs/phase-6.md`.
- **Done when:** on one laptop, profile `home` creates the group and an invite; profile `carol` joins with the link, receives full history and the group key, and writes her profile after her own `member_add` arrives. Tests: the joiner sends nothing but `INVITE_REDEEM` before `INVITE_OK`; second use of a link by another pubkey fails; retry by the same pubkey after a dropped `INVITE_OK` succeeds; a crash between `member_add` and marking the token (simulated by raising inside the transaction) leaves the token usable; an expired link fails; an `INVITE_OK` with the wrong group or admin, or a key that won't unseal, is rejected and nothing is saved; a non-member receives no `HEADS` and is closed on any message except `INVITE_REDEEM`; posts by non-members are rejected; a fork detected via `HEADS` hash mismatch shows in `status`.

### Phase 7: Web UI

- **Build:** `web.py`, `feed.py` and `static/` (`index.html`, `app.js`, `style.css`); `run` starts the web UI unless `--headless`.
- **API (all JSON, all require `X-Hearth-Token`):** `GET /api/me`, `POST /api/profile`, `GET /api/feed?before=<sort_ts>:<author>:<seq>&limit=50` (composite cursor compared lexicographically on the stored `sort_ts`, then author, then seq; because `sort_ts` never changes, ties and future-dated posts never skip or duplicate), `POST /api/posts` (multipart: text + up to 4 images), `GET /api/image/<author>/<seq>/<index>` (validated parameters; fetches missing chunks via `ChunkFetcher`, waits up to 30 s), `GET /api/status` (home connection, last sync time, outbox count, forked authors).
- **UI:** a single page with a compose box (text + image picker with previews), the feed (newest first, author name, relative time from `sort_ts`, images loaded through `fetch` + object URLs), posts still in the outbox shown as "waiting to upload" (or "home node is full"), a status pill ("connected to home" / "offline"), a fork warning banner when relevant, and a settings panel for the display name. Poll `/api/feed` every 5 seconds; no websockets.
- **Done when:** the owner posts from the browser and sees a friend's post (another profile) appear without refreshing; images display; a post dated in the future by a test helper shows at its arrival time and stays in the same feed position across repeated requests. All security headers are present; requests without the token, with a bad `Host`, or with invalid path parameters are refused.

### Phase 8: Home node and real network

- **Build:** `--headless --role home --passphrase-file` mode, `deploy/hearth-home.service`, `docs/deploy.md`, and a `backup` command using `sqlite3.Connection.backup()` that never includes the passphrase file.
- **Details:** benchmark Argon2id `MODERATE` on the VM; switch the home node to `INTERACTIVE` only if startup is unacceptable, and record the justification in `docs/phase-8.md`.
- **Done when:** the home node runs on the on-prem VM under systemd and restarts after a reboot; two laptops on different networks (for example home Wi-Fi and a phone hotspot) both sync posts with images through it; a laptop that was offline for a day catches up on reconnect; a backup taken while the service runs restores cleanly.

## Personality in the code

The source should be fun to read, but the fun never hides how the code works and never touches security logic.

**Encouraged:**

- A short joke or pun in each module docstring. Example for `sync.py`: "Gossip, but make it cryptographically verified. Aunties everywhere are jealous."
- A themed ASCII-art banner (a small hearth or campfire) printed by the CLI on `run`, with a random tagline from a list in `config.py`, such as "No servers were harmed in the making of this forum."
- Friendly, slightly cheeky log and error messages, as long as they still say exactly what went wrong: `"Bob's entry #12 has a forged signature. Nice try, Bob."`, `"Frame too big (3.1 MiB > 2 MiB). We're a forum, not a moving truck."`
- Easter eggs with no functional effect, for example `python -m hearth chai` prints a tiny ASCII cup and a random tip from the codebase; a `PING` counter that logs a celebratory line on the 1,000th pong.
- Fun test names: `test_bob_cannot_pretend_to_be_alice`, `test_replay_attack_gets_left_on_read`.
- Occasional comments that explain a concept with an analogy: "The hash chain is like page numbers in a diary glued with superglue: tear one out and everyone notices."
- A `HALL_OF_FAME` comment at the top of `__main__.py` where the owner can list friends who joined.

**Rules:**

- Humour lives in docstrings, comments, log text, CLI output and test names, never in variable or function names. Names stay plain and descriptive.
- Error messages shown to users stay clear first, funny second. Put the actual reason before the joke.
- No jokes about real people, religion, politics or anything mean. Keep it friend-group friendly.
- Never joke inside code that does cryptography or validation in a way that makes the logic harder to follow.

## Home node deployment

The home node runs on the owner's on-prem VM (Linux assumed) as a dedicated user under systemd, listening on TCP 7777.

**Reachability (the one thing to confirm early):** friends' laptops outside the owner's network must reach the VM's port 7777.

- If the VM's network has a public IP: forward TCP 7777 on the router or firewall to the VM.
- If the ISP uses carrier-grade NAT (no public IP), port forwarding won't work. Fallback: a small cloud VPS running only a WireGuard tunnel that forwards 7777 to the VM. The VPS can't read post content, but it does see the plaintext handshake (pubkeys), connection times and traffic volume. `docs/deploy.md` must cover both paths.
- **The tunnel must preserve client source IPs:** use DNAT on the VPS without MASQUERADE, and policy routing on the VM so replies go back through the tunnel. With MASQUERADE, every member appears to come from the tunnel IP, so the 8-connections-per-IP limit would cap the whole group at 8 nodes and one person's failed invite retries would lock out everyone. Record this in `docs/decisions.md`.
- A dynamic IP is fine with a free dynamic-DNS hostname; invite links should use the hostname, not a raw IP.

**Setup steps for `docs/deploy.md`:**

1. Create a system user `hearth` with no login shell. systemd's `StateDirectory=hearth` creates and owns `/var/lib/hearth`.
2. Install Python 3.12+, clone the repo to `/opt/hearth`, create a virtualenv, `pip install .`.
3. As `hearth`: `python -m hearth init --profile home --data-dir /var/lib/hearth`, then `create-group`.
4. Store the passphrase in `/etc/hearth/passphrase` (owner `hearth`, mode `0600`).
5. Install `deploy/hearth-home.service` and enable it.
6. Allow only TCP 7777 inbound in the VM firewall (`ufw allow 7777/tcp`); SSH limited to the owner's network.
7. Create invites over SSH: `sudo -u hearth python -m hearth invite --data-dir /var/lib/hearth`. Invite links use the public hostname (dynamic DNS if needed), never a raw IP.

**systemd unit requirements:** `User=hearth`, `StateDirectory=hearth`, `Restart=on-failure`, `RestartSec=5`, `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `PrivateDevices=yes`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, `MemoryDenyWriteExecute=yes`, `CapabilityBoundingSet=` (empty), `ExecStart=/opt/hearth/.venv/bin/python -m hearth run --headless --role home --listen 0.0.0.0:7777 --data-dir /var/lib/hearth --passphrase-file /etc/hearth/passphrase`. Check that `MemoryDenyWriteExecute` doesn't break PyNaCl or Python on the VM; drop it with a note in `docs/deploy.md` if it does.

**Operations:**

- Logs via `journalctl -u hearth-home`. `python -m hearth status` (built in Phase 6) shows member count, connected peers, entries per author, forked authors, outbox size, and chunk storage against the quota.
- Back up nightly with `python -m hearth backup --out <file>`, which uses `sqlite3.Connection.backup()` for a consistent snapshot while the service runs, plus a copy of `chunks/`, `identity.key` and `group.key`. Never copy `hearth.db` directly while it's running (WAL makes the copy torn). Never include `/etc/hearth/passphrase` in a backup; keep the passphrase separately (for example in the owner's password manager). Losing `identity.key` means losing admin rights, so keep an offline copy.
- Storage grows without limit except the quota (up to 40 MiB per post, no deletion). Watch `status` and raise `storage_quota_gib` deliberately; backups grow with it.
- Updates: `git pull`, `pip install .`, `systemctl restart hearth-home`.

## Testing and definition of done

Every module has a test file, and the MVP is done only when the three-node end-to-end test and the manual two-network check both pass.

**Test layers:**

- **Unit tests** per module: canonical JSON stability and strictness, signing and verification, every rejection reason including the `head` rules, key file format and KDF bounds, encryption round-trips, chunk hashing and magic bytes, schema validation, invite token rules, feed cursor ordering with stable `sort_ts`.
- **Security tests**, one per rule in the Security section: forged signature, fork detection (by conflicting entry and by `HEADS` hash), replayed or tampered frame, oversized frame, non-member messages, joiner sending anything before `INVITE_OK`, reused and expired invites, invite retry by same and different pubkey, wrong `INVITE_OK` fields, path traversal attempt with a fake chunk ID or API parameter, web API request without token, wrong `Host` header, insecure passphrase file permissions, out-of-range KDF parameters.
- **Integration tests** with real nodes in one process on random localhost ports, each with a temporary data dir: handshake, sync, multi-frame sync, the offline-relay scenario through the home node, invite-and-join (both sides of the state machine), image transfer, the poster-goes-offline availability case, and the storage quota.
- **CLI smoke tests** (`test_cli.py`): `init`, `debug sign`, `status` run as subprocesses with `--passphrase-file`.
- **Manual checks** listed in each `docs/phase-N.md` "try it yourself" section.

**Test helpers (`tests/conftest.py`):** `make_node(tmp_path, name, passphrase="hunter2")` creates the identity through `Identity.create(passphrase)` (no prompts, no subprocess), starts a node with short injected timeouts (around 0.05 s) and returns it; `eventually(condition, timeout=5)` polls for async assertions instead of fixed sleeps.

**Definition of done for the MVP:**

- [ ] All tests pass; no test uses `time.sleep` for synchronisation
- [ ] CI green: `pytest`, `bandit`, `pip-audit` clean or findings justified
- [ ] Home node running on the VM under systemd, surviving a reboot
- [ ] Two laptops on different networks sync posts with images through it
- [ ] A friend joins with only an invite link and sees full history
- [ ] An image survives its poster going offline right after posting
- [ ] The SQLite file and chunk folder contain no readable content
- [ ] `docs/phase-1.md` to `docs/phase-8.md`, `docs/decisions.md` and `docs/deploy.md` written
- [ ] README explains setup in under 10 commands

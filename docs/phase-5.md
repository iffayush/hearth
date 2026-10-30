# Phase 5: Group encryption and images

> **Changed in Phase 6:** `debug join-group` and `debug show-group-key` were replaced by `invite` and `join`. See `phase-6.md`.

## What was built

Posts are now unreadable to anyone outside the group, and they can carry images that stay available even after their poster goes offline.

**Encryption** (`crypto.py`). Every group has one random 32-byte key, kept in `group.key` (`group.py`, same file format as `identity.key`, tag `hearth-group-v1`). A post body is now just `{"box": ...}`: `seal_box()` encrypts `{"text", "images"}` with SecretBox and a fresh random nonce. The store can't see inside a box, so `feed.open_box()` checks the contents every time a post is opened. That means text up to 10,000 characters, at most 4 images, and well-formed manifests. Anything else shows as "unreadable".

**Images** (`chunks.py`). `detect_image_type()` reads magic bytes (JPEG, PNG, WebP, GIF; never the file name). `split_image()` cuts the image into 1 MiB pieces and encrypts each one; a chunk's ID is the BLAKE2b of its ciphertext. `reassemble()` checks every chunk hash, decrypts, joins, and checks the whole-image hash. `ChunkStore` keeps chunks under `chunks/ab/abcd…`, building paths only from validated hex IDs.

**The delivery guarantee** (`outbox.py`, `node.py`). An image post on a member's node doesn't enter the log straight away. `actions.post()` saves the encrypted chunks locally and queues the encrypted post in the `outbox` table. While connected to the home node, `Node._push_outbox()` sends CHUNK_PUSH for every unacknowledged chunk. The home node's `Syncer._on_chunk_push()` checks membership, hash and quota, stores the chunk, and replies CHUNK_ACK. When a post's last chunk is acknowledged, `Node._on_chunk_ack()` signs it into the log. So any image post in any log has its chunks on the home node. Over quota, the home node sends `ERROR quota_exceeded`; the post stays queued and the poster backs off for 10 minutes.

**Fetching** (`fetcher.py`). Members fetch chunks only when someone looks at an image. `ChunkFetcher.fetch()` asks the home node for at most 4 chunks at a time, with a 10 s timeout and 3 tries. It keeps one download per chunk ID, so two viewers share a transfer, and it verifies every chunk before saving it.

**Who's the home node?** The admin. A member treats the connected peer whose key is the admin's as home, and only a node whose `config.json` says `"role": "home"` accepts uploads.

**CLI.**
- `create-group` replaces `debug create-group` and also writes `group.key`.
- `post "text" --image FILE` can be repeated for up to 4 images.
- `feed` now decrypts, so it asks for your passphrase when no node is running.
- `debug image AUTHOR SEQ INDEX --out FILE` saves one image from a post.
- Joining is a stand-in until Phase 6's invites: the admin runs `debug show-group-key`, and the member pastes it into `debug join-group` at a hidden prompt.

**Delete your Phase 4 data directories.** Post bodies changed, so the database schema is now version 2, and old folders are refused with a clear message.

## Try it yourself

```
python -m pytest
```
Expect `338 passed, 2 skipped` (Windows) or `340 passed` (Linux).

Delete `~/.hearth/alice` and `~/.hearth/bob`, then `init` both again. Then:
```
python -m hearth create-group --profile alice
python -m hearth debug add-member <bob's ID> --profile alice
python -m hearth debug show-group-key --profile alice
python -m hearth debug join-group <group ID> <alice's ID> --profile bob
```
The last command asks for bob's passphrase, then the group key: paste the 64 hex characters (they won't echo). Make alice the home node: open `~/.hearth/alice/config.json` and change `"role": "member"` to `"role": "home"`. Run alice with `run --profile alice --listen 127.0.0.1:7001`, and bob with `run --profile bob --dial 127.0.0.1:7001 --dial-pubkey <alice's ID>`. In a third terminal:
```
python -m hearth post "noise, but make it art" --image C:\path\to\photo.jpg --profile bob
```
This prints `Waiting to upload: 1 image(s) in 2 chunk(s)…`. Bob's log shows `Uploading 2 chunk(s)…` then `Image post uploaded; it's #1 in your log now`. Then:
```
python -m hearth feed --profile alice
python -m hearth debug image <bob's ID> 1 0 --out rebuilt.jpg --profile alice
```
`feed` shows the post with `[1 image(s): …]`. `debug image` saves `rebuilt.jpg`, identical to your photo.

## Poke it

1. **Read the database.** Open `~/.hearth/alice/hearth.db` in any SQLite viewer (or `python -c "print(open(r'C:\Users\you\.hearth\alice\hearth.db','rb').read()[:3000])"`). There's no post text anywhere, only base64 boxes. `debug log` shows `(encrypted post, … characters of ciphertext)`.
2. **Disguise an .exe as a photo.** Copy any `.exe` to `cat.png` and try `post "cute" --image cat.png`: `error: cat.png: that's not a JPEG, PNG, WebP or GIF (judging by its first bytes, not its name)`.
3. **Damage a chunk on the home node.** With alice running, flip a byte in one of her chunk files (any file under `~/.hearth/alice/chunks/`):
   `python -c "import pathlib; p = next(f for f in pathlib.Path.home().joinpath('.hearth/alice/chunks').rglob('*') if f.is_file()); d = bytearray(p.read_bytes()); d[100] ^= 1; p.write_bytes(d)"`
   Now `debug image <bob's ID> 1 0 --out again.jpg --profile alice` refuses: `some of this image's chunks aren't stored on this node`. Alice's log explains: `Chunk 1c3502ee on disk doesn't match its ID; ignoring our copy`. A damaged chunk is never served or shown; members asking for it would get CHUNK_MISSING. (The quota has its own automated tests: `test_push_over_the_quota_keeps_the_post_in_the_outbox`.)

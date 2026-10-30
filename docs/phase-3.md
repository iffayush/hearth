# Phase 3: Two nodes talk

## What was built

Two nodes can now find each other over TCP, prove who they are, agree on fresh encryption keys, and keep the line alive with PING/PONG. There's no syncing yet (that's Phase 4).

**Framing** (`protocol.py`). Every frame is a 4-byte length plus payload. `read_frame()` refuses any length over 2 MiB *before* reading the payload. `parse_message()` checks each message against `SCHEMAS`: known type, exactly the expected fields, and correct formats. Anything else raises `ProtocolError`.

**Handshake** (`peer.py`, `Peer._run_handshake`). Each side sends HELLO (long-term pubkey, a fresh ephemeral X25519 key, a random nonce). It then checks the reply in `_check_hello`: protocol version, "not ourselves", and the pinned key if we dialled with one. Next, each side signs `handshake_transcript()` and sends AUTH. Finally `derive_session_keys()` runs `crypto_kx` and the ephemeral private key is dropped. The module docstring spells out the exact rx/tx mapping, and `test_crypto_kx_key_mapping` checks it.

**Session** (`peer.Session`). After the handshake every frame is SecretBox-encrypted, with the frame counter as the nonce. `Session.open()` advances the counter *before* decrypting. That one line is why a replayed, reordered or tampered frame fails and closes the connection, while a frame that decrypts to junk JSON is merely dropped (`Peer._decrypt`).

**Node** (`node.py`). It listens with `asyncio.start_server`. Inbound connections are capped at 64 in total and 8 per IP address. For each dial target, `_dial_forever()` redials with backoff (`next_delay`, `jittered`). Every timeout is a constructor argument, which is how the tests run at 0.05 s.

**CLI.** `run` holds the data-dir lock, prints the campfire banner, and runs the node until Ctrl+C.

Data flow for one PING: `_ping_loop` → `Peer.send` → `Session.seal` → `encode_frame` → TCP → `read_frame` → `Session.open` → `parse_message` → `_handle` → PONG back the same way.

**Bandit:** two `# nosec B311` lines, both for `random`: backoff jitter and the banner tagline. Neither is a security decision, and the spec allows `random` for exactly these. **Old data directories:** nothing changed on disk, so keep them.

## State machine (Phase 3)

Both sides run the same table. Phase 6 adds `LIMITED` (home node, unknown peer) and `JOINING` (joiner).

| Side | State | Event | Next state | Action |
| --- | --- | --- | --- | --- |
| both | (connected) | — | `AWAIT_HELLO` | send own `HELLO` |
| both | `AWAIT_HELLO` | valid `HELLO`: right proto, not our own pubkey, matches the pin if one is set | `AWAIT_AUTH` | send own `AUTH` |
| both | `AWAIT_AUTH` | valid `AUTH`, authorizer says yes (always, in Phase 3) | `ACTIVE` | derive keys, start pinging |
| both | `AWAIT_AUTH` | valid `AUTH`, authorizer says no | `CLOSED` | derive keys, send `ERROR not_member` |
| both | `AWAIT_*` | bad frame, bad signature, pin mismatch, wrong proto, 10 s timeout | `CLOSED` | log reason (no `ERROR`: no encrypted channel yet) |
| both | `ACTIVE` | `PING` | `ACTIVE` | send `PONG` |
| both | `ACTIVE` | `PONG` | `ACTIVE` | count it |
| both | `ACTIVE` | frame decrypts, but bad JSON or unknown type | `ACTIVE` | log and drop |
| both | `ACTIVE` | frame fails to decrypt, or is over 2 MiB | `CLOSED` | send `ERROR bad_frame` |
| both | `ACTIVE` | `ERROR` received | `CLOSED` | log it |
| both | `ACTIVE` | nothing received for 60 s | `CLOSED` | log it |

## Try it yourself

```
python -m pytest
```
Expect `186 passed, 2 skipped`.

Create bob if you haven't: `python -m hearth init --profile bob`. Then, in terminal 1:

```
python -m hearth run --profile alice --listen 127.0.0.1:7001
```
You'll see the banner, a tagline, and `INFO hearth.node: Listening on 127.0.0.1:7001 as <alice8>`. In terminal 2:

```
python -m hearth run --profile bob --listen 127.0.0.1:7002 --dial 127.0.0.1:7001
```
Within a second, both terminals log `Handshake complete with <the other's 8 hex>` and `PONG from <the other's 8 hex>: the line is alive`. Add `--verbose` to see every later PING/PONG (every 20 s). Press Ctrl+C in either terminal and it prints `Node stopped. The fire's out…`. If you stop alice, bob logs `Can't reach 127.0.0.1:7001` and keeps retrying.

## Poke it

1. **Pin the wrong key.** Restart bob with `--dial-pubkey aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`. Bob logs `<alice8> at 127.0.0.1:7001 doesn't match the pinned key aaaaaaaa; aborting`, then `Will try … again in 1.1 s`, `2.3 s`, `3.4 s`, `7.7 s`… (doubling, ±20% jitter, capped at 60 s). Now pin alice's real key (from `init`'s output) and it connects.
2. **Send a moving truck.** With alice running:
   `python -c "import socket; s = socket.create_connection(('127.0.0.1', 7001)); s.sendall((3 * 2**20).to_bytes(4, 'big')); print(len(s.recv(1000)), 'bytes back')"`
   Alice logs `bad handshake frame … Frame too big (3.0 MiB > 2 MiB). We're a forum, not a moving truck.` and carries on serving bob.
3. **Say nothing.** `python -c "import socket, time; s = socket.create_connection(('127.0.0.1', 7001)); time.sleep(15)"`. After 10 seconds alice logs `handshake with 127.0.0.1:… timed out`.

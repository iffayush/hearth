# Phase 6: Membership and invites

## What was built

A friend can now join with one link: no email, no phone number, no password. Only members get in; everyone else gets a polite door in the face.

**Invites** (`invite.py`). On the home node, `create_invite()` makes 32 random bytes, stores only their BLAKE2b hash with an expiry, and returns `hearth://join?h=…&k=…&g=…&t=…`. A joiner's node dials `h`, accepts only the node whose key is `k`, and sends INVITE_REDEEM with the token and its X25519 public key. On the home node, `redeem()` checks the token (known, unused, unexpired), signs the admin's `member_add`, and seals the group key to the joiner's key. `Store.redeem_invite()` then claims the token, appends the entry and keeps the sealed key **in one transaction**: a crash anywhere in the middle leaves the token unused and no `member_add` behind. The joiner's `open_invite_ok()` checks the group, the admin and that the key unseals *before* `Node._on_joined()` saves anything.

**Single use, no retries (your call).** A token works once, for anyone. If an INVITE_OK is lost, the joiner's identity is already a member without the key. Joining again with that identity stops with: ask for a new invite and join with a fresh profile.

**Who gets in** (`peer.py`, `group.is_member`). After the handshake, a member (the admin or anyone in the membership table) goes to ACTIVE. On the home node a stranger goes to LIMITED: nothing is sent to it, and anything but INVITE_REDEEM ends with `ERROR not_member`. On any other node a stranger is simply refused. `Node._on_invite()` allows 5 redemption attempts per IP address per 10 minutes. The handshake helpers moved to `session.py`, and the address and connection-limit plumbing to `network.py`, to keep files under 400 lines.

**CLI.**
- `invite [--expires 48h] [--host name:port]` runs on the running home node. The host defaults to `public_host` in `config.json`.
- `join "LINK" [--name Carol]` creates the identity and redeems the link. It waits for the admin's log up to its own `member_add`, then writes the profile entry with the display name, and saves the home node's address and key into `config.json`, so `run` needs no flags.
- `status` shows members, logs per author (with names), forks, the outbox, storage against quota and connections.
- `feed` now shows display names.
- `debug join-group`, `debug show-group-key` and `debug add-member` are gone.

**Old data directories:** nothing on disk changed since Phase 5 (`config.json` gained an optional `public_host`), so Phase 5 folders work.

## The state machine, both sides

| Side | State | Event | Next state | Action |
| --- | --- | --- | --- | --- |
| both | (connected) | — | `AWAIT_HELLO` | send own `HELLO` |
| both | `AWAIT_HELLO` | valid `HELLO`: right proto, not our own key, matches the pin if set | `AWAIT_AUTH` | send own `AUTH` |
| home | `AWAIT_AUTH` | valid `AUTH`, peer is admin or member | `ACTIVE` | derive keys, send `HEADS`, start pinging |
| home | `AWAIT_AUTH` | valid `AUTH`, peer unknown | `LIMITED` | derive keys, send nothing |
| home | `LIMITED` | `INVITE_REDEEM`, token valid | `ACTIVE` | commit `member_add`, send `INVITE_OK`, then `HEADS` |
| home | `LIMITED` | `INVITE_REDEEM`, token invalid, used or expired | `CLOSED` | send `ERROR bad_invite` |
| home | `LIMITED` | 6th `INVITE_REDEEM` from one IP in 10 min | `CLOSED` | send `ERROR rate_limited` |
| home | `LIMITED` | any other message | `CLOSED` | send `ERROR not_member` |
| member | `AWAIT_AUTH` | valid `AUTH`, pinned key matches, peer is admin or member | `ACTIVE` | derive keys, send `HEADS` |
| member | `AWAIT_AUTH` | valid `AUTH`, peer unknown | `CLOSED` | send `ERROR not_member` |
| joiner | `AWAIT_AUTH` | valid `AUTH`, key matches the link's `k` | `JOINING` | derive keys, send `INVITE_REDEEM`, nothing else |
| joiner | `JOINING` | `INVITE_OK` with `group == g`, `admin == k`, key unseals | `ACTIVE` | save group key and `meta`, send `HEADS`, start syncing |
| joiner | `JOINING` | `INVITE_OK` that fails any check | `CLOSED` | abort, save nothing |
| joiner | `JOINING` | `HEADS` or `PING` (we're already a member, key lost) | `CLOSED` | say: new invite, fresh profile |
| joiner | `JOINING` | `ERROR`, any other message, or 15 s timeout | `CLOSED` | report; a retry of the same link may work |
| any | `ACTIVE` | `PING` / `PONG` / sync and chunk messages | `ACTIVE` | as in Phases 3–5 |
| any | any | pinned key mismatch, timeout, bad frame or signature | `CLOSED` | log reason (WARNING if suspicious) |

## Try it yourself

```
python -m pytest
```
Expect `389 passed, 2 skipped` (Windows) or `391 passed` (Linux).

Start fresh (delete `~/.hearth/home` and `~/.hearth/carol` if they exist):
```
python -m hearth init --profile home
python -m hearth create-group --profile home
```
In `~/.hearth/home/config.json` set `"role": "home"` and `"public_host": "127.0.0.1:7001"`. Then run it with `python -m hearth run --profile home --listen 127.0.0.1:7001`, and in a second terminal:
```
python -m hearth post "Welcome to the hearth. Kettle's on." --profile home
python -m hearth invite --profile home
python -m hearth join "<the link>" --profile carol --name Carol
```
`invite` prints the link (plus a nudge to use a hostname in real life). `join` asks for carol's new passphrase twice, then prints `Welcome to the group, Carol! You now hold the group's 4 entries.` Then:
- `feed --profile carol` shows the welcome post.
- `status --profile home` shows `members: 2` and `Carol  1 entry`.
- Running `run --profile carol` with no flags dials home pinned.

## Poke it

1. **Use a link twice.** Run the same `join` again with `--profile mallory`: `error: the invite didn't work: the home node refused: ERROR bad_invite: 'that invite has already been used'. Ask the admin for a new link.` Home logs it as a WARNING.
2. **Knock without an invite.** Run `init --profile eve` and `run --profile eve --dial 127.0.0.1:7001`. Neither side lets the other in. Home logs `<eve8> (…) isn't a member; it may only redeem an invite`, then a WARNING: `Closed connection with <eve8>: <eve8> isn't a member and sent ERROR`. Eve's node doesn't know home either, so it logs `Closed connection with <home8>: <home8> isn't a member of this group`, then backs off and retries (`Will try … again in 0.9 s`, `1.8 s`…). Not one HEADS crosses the wire.
3. **Let one expire.** `invite --expires 1m --profile home`, wait a minute, then `join` it: `ERROR bad_invite: 'that invite has expired'`.

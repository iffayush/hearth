# Security

Hearth uses well-established primitives from libsodium, through PyNaCl. How it combines them is custom, and **has not been independently audited**. The tests show the code behaves as intended in the scenarios they cover. They don't prove the protocol is secure.

## Reporting a vulnerability

Please **don't open a public issue**. Contact the repository owner privately, with:

- what the problem is, and what an attacker could do with it;
- steps to reproduce, ideally against a throwaway group;
- the version or commit.

Never send real passphrases, key files, invite links or backups, even in a report.

Only the latest commit on `main` is supported. There are no maintained older releases.

## What Hearth protects

**The goal:** keep content away from anyone outside the group who watches the network or relays traffic, and detect forged or altered posts.

| An attacker has… | Result |
| --- | --- |
| A view of the network, or a relay in the path (ISP, VPS, Tailscale) | Can't read content after the handshake. Learns who connects, when, and roughly how much. |
| A member's data directory or disk, without the passphrase | Keys and content stay protected. Metadata is readable. |
| A copy of the home node's data or backups, without its passphrase file | Keys and content stay protected. Metadata is readable. |
| A forged or altered entry to inject | Rejected: every entry is signed and chained to its author's previous one. |
| A stranger's connection to the home node | Reaches a limited state where the only accepted message is an invite redemption, with limited attempts. |

## What Hearth does not protect

- **A malicious member.** Members hold the group key and keep what they've received. A member can also backdate their own posts.
- **Cutting someone off.** There's no member removal or key rotation, so anyone invited can read all future posts for good.
- **The home node's machine.** A full image of the VM's disk includes the passphrase file, which unlocks everything. That also covers anyone with admin rights on the host running the VM. Encrypting the disk at the hypervisor narrows this, but Hearth makes no claim here.
- **Your own device while it's in use.** Malware on a member's machine sees what the member sees.
- **Your IP address.** Nothing hides it from the home node, or from whoever runs the home node's route.

## Metadata that isn't hidden

- **At rest:** anyone who copies a node's disk learns author public keys, membership, entry kinds, timestamps, sizes and the home node's hostname, with no passphrase needed.
- **On the network:** `HELLO` and `AUTH` are plaintext and carry public keys, so an observer can tell who connects to the home node.
- **Public names:** a Tailscale Funnel name appears in public certificate transparency logs. Expect crawlers within minutes. They can't get past the handshake without an invite.

## How it's built

**Cryptography**

- Signatures: Ed25519, with a domain-separation prefix for each context (`hearth/entry/v1`, `hearth/handshake/v1`).
- Session keys: fresh X25519 keys for each connection through `crypto_kx`, separate for each direction, with counter nonces. Ephemeral private keys are dropped after the handshake.
- Content: SecretBox (XSalsa20-Poly1305) with one group key and random nonces.
- Joining: the group key is sealed to the joiner's X25519 key with SealedBox, inside the authenticated session.
- Keys at rest: Argon2id plus SecretBox, in tagged files. Only libsodium's `INTERACTIVE` and `MODERATE` parameters are accepted when loading, so a tampered file can't demand an expensive derivation.
- Hashes: BLAKE2b-256. Randomness comes only from `nacl.utils.random` or `secrets`.

**Untrusted input**

- Every network message is checked against an explicit schema before use. JSON goes through one strict parser that rejects floats, duplicate keys and booleans used as integers. There's no `pickle`, `eval`, `exec` or `yaml.load`.
- Limits apply before work happens: a frame's length is checked before its payload is read.
- Chunk paths come only from validated hex IDs and must resolve inside `chunks/`. SQL is always parameterised.
- One misbehaving peer closes its own connection and can't take the node down.

**The local web UI**

- It binds only to `127.0.0.1`, and refuses to start otherwise.
- A random per-launch token goes in a header on every API call, with no cookies, so other websites can't call the API. The `Host` header is checked to block DNS rebinding.
- A strict Content Security Policy applies, and post text is rendered with `textContent`, never `innerHTML`.

**Tailscale Funnel**

- The outer TLS is only the envelope Funnel requires. Its certificate isn't checked, because Hearth's handshake inside pins the home node's key from the invite link.
- `--proxy-protocol` believes the address in a PROXY header without proof, so `run` only allows it on a loopback listener that only the front door can reach.

**The home node's service**

- It runs as an unprivileged user under a hardened systemd unit (`systemd-analyze security` rates it 1.5).
- Backups never contain the passphrase.

**Secrets in logs**

- Keys, tokens, passphrases, post text and decrypted images are never logged. Public keys are shortened to 8 hex characters.

## The test certificate in this repository

`tests/data/front-door-test.pem` holds a private key and a self-signed certificate on purpose. It exists only for the tests' stand-in front door, and nothing trusts it. Secret scanners may flag it; that's expected.

## For anyone running a home node

- Keep the passphrase in a password manager. Don't leave a copy in the project folder or anywhere it could be committed.
- Copy backups off the VM, and test a restore.
- Run a group only on hardware whose admin you trust with everything in it.
- Tell members about the limits above **before** inviting them.

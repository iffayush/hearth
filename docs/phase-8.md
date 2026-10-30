# Phase 8: Home node and real network

## What was built

The home node can live on a server, reachable from anywhere.

- **`run --role home`** makes the admin's profile the home node and saves that in `config.json`; members are refused. Image posts written before the switch publish at once.
- **Fit for systemd:** `run` stops cleanly on SIGTERM, and stdout is line-buffered for the journal. `deploy/hearth-home.service` sandboxes it: `systemd-analyze security` rates it 1.5 OK.
- **`backup --out FILE`** writes one `.tar` while the node runs: a SQLite online-backup snapshot, the key files, `config.json` and every image chunk. Never the passphrase. `debug verify` now re-hashes every chunk too.
- **`debug bench-kdf`** times Argon2id on this machine.
- **`docs/deploy.md`** covers install, firewall, invites, backups and restore, plus three ways for friends to reach the VM: port forwarding (A), a WireGuard VPS that keeps friends' real IPs (B), or Tailscale Funnel for a VM that's only on a tailnet (C).
- **Path C needed code** (`transport.py`):
  - an address can be `tls://host:port`, in `--host`, `config.json` and invite links, and friends' nodes wrap that connection in TLS;
  - `run --proxy-protocol` reads the PROXY v2 header Funnel sends, so the per-IP limits count each friend, not the front door. `run` allows it only on a loopback address.

  The certificate isn't checked: Hearth's handshake already pins the home node's key. Friends never install Tailscale.
- **Fixed:** `status` crashed with no node running.

Tested in Ubuntu 24.04 containers: every `deploy.md` step as written, a reboot, path B, and path C with HAProxy standing in for Funnel. **Old data directories:** unchanged.

## Try it yourself

```
python -m pytest
```
Expect `508 passed, 5 skipped` (Windows; the skips are POSIX-only) or `513 passed` (Linux, as root).

A home node behind a pretend front door, on port 7788 so it can't clash with a home node you already run:
```
python -m hearth init --profile demo8
python -m hearth create-group --profile demo8
python -m hearth run --profile demo8 --role home --listen 127.0.0.1:7788 --proxy-protocol --headless
```
Expect a line saying caller addresses come from the PROXY v2 header, `Saved role 'home' in config.json…` and `Listening on 127.0.0.1:7788`. In a second terminal:
```
python -m hearth invite --profile demo8 --host tls://hearth-vm.tail1234.ts.net:443
python -m hearth backup --profile demo8 --out demo8.tar
```
The link starts `hearth://join?h=tls://hearth-vm.tail1234.ts.net:443&k=`. The backup reports `Backed up 1 entry and 0 image chunks`, and that the passphrase isn't in it.

## Poke it

1. **Make the front door public.** Restart the node with `--listen 0.0.0.0:7788`: it refuses, because a PROXY header is believed blindly.
2. **Back up twice to one name.** Repeat the `backup`: `demo8.tar already exists; pick a new name`. Old backups are never overwritten.
3. **Skip the door.** In a third terminal, `python -m hearth init --profile stray`, then `python -m hearth run --profile stray --dial 127.0.0.1:7788 --headless`. The home node logs `Dropped a connection from 127.0.0.1:…: it didn't start with a PROXY v2 header`.

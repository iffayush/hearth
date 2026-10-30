<p align="center">
  <img src="drawables/banner.png" alt="Hearth" width="100%">
</p>

<p align="center">
  <strong>A private, invite-only forum that lives on its members' own devices.</strong><br>
  No company, no public website, no accounts on anyone else's server.<br>
  Every member runs the same small Python program, and all posts and photos are end-to-end encrypted.
</p>

<p align="center">
  <a href="https://github.com/iffayush/hearth/actions/workflows/ci.yml"><img src="https://github.com/iffayush/hearth/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-orange" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-orange" alt="Python 3.12 or newer">
  <img src="https://img.shields.io/badge/status-development-red" alt="Status: development">
</p>

> [!WARNING]
> **This is a development project, not a finished product.** It's a small build tested on a handful of machines, with a custom protocol nobody has audited. Don't trust it with anything you can't afford to lose or leak.
>
> **Think the protocol could be better? Found a way to break it? Don't just say so: open a PR.** See [Think you can do better?](#think-you-can-do-better)

| | |
| --- | --- |
| **Status** | Version 0.1.0, a development project. All 8 build phases are complete, and a home node has run on a real VM. Some real-network checks are still open: see [`PROGRESS.md`](PROGRESS.md). |
| **Scale** | 5 to 50 people who trust each other |
| **Runs on** | Python 3.12+. Tested on Linux and Windows. macOS is listed in CI but has never been run. |
| **Security** | Built on libsodium, but the protocol is custom and has **not** been independently audited. Read [`SECURITY.md`](SECURITY.md) before relying on it. |

## Contents

- [What it does](#what-it-does)
- [What you need to know before inviting anyone](#what-you-need-to-know-before-inviting-anyone)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Command reference](#command-reference)
- [Configuration and files](#configuration-and-files)
- [Limits](#limits)
- [Documentation](#documentation)
- [Project layout](#project-layout)
- [Development](#development)
- [Think you can do better?](#think-you-can-do-better)
- [Not built yet](#not-built-yet)
- [License](#license)

## What it does

- **One link to join.** An admin sends a single-use invite link. No email, phone number or password with anyone else.
- **Posts and photos.** Up to 10,000 characters and 4 images per post (JPEG, PNG, WebP or GIF, up to 10 MiB each).
- **A merged feed in your browser.** Each member reads and posts at a local page on `127.0.0.1`, protected by a per-launch token.
- **Sync without being online together.** A nonstop home node relays everything, so two friends never need to be awake at the same time.
- **End-to-end encryption.** Content is encrypted with a group key that only members hold. A node without it sees only ciphertext.
- **Tamper evidence.** Every entry is signed and hash-chained to its author's previous entry. Forged or altered posts are rejected.
- **Works behind difficult networks.** The home node can be reached by port forwarding, through a WireGuard VPS, or through Tailscale Funnel. Friends never need to install Tailscale.
- **Runs as a locked-down service.** The home node ships with a hardened systemd unit, and online backups that never include the passphrase.

## What you need to know before inviting anyone

"Private" here means **unreadable to anyone outside the group**. It has real limits, and everyone you invite should hear them first:

- **Every member is fully trusted.** Members hold the group key and keep every post they've received.
- **You can't remove anyone.** There's no member removal or key rotation, so a member can read all future posts for good.
- **Posts can't be edited or deleted.** Logs are append-only, on everyone's device.
- **A lost passphrase can't be recovered.** Nobody can reset it.
- **Whoever controls the home node's machine can read everything.** Its disk holds both the keys and the file that unlocks them.
- **Metadata isn't hidden.** Who is in the group, who posted when and how much is visible on any node's disk, and connection patterns are visible on the network.

The full threat model is in [`SECURITY.md`](SECURITY.md).

## How it works

The forum is a set of per-member, append-only logs. There's no shared database: each node merges the logs it holds into a feed.

```
  friend's laptop                 home node (always on)                friend's laptop
 ┌────────────────┐              ┌──────────────────────┐             ┌────────────────┐
 │ browser        │              │ group admin          │             │ browser        │
 │   ▲ 127.0.0.1  │    Hearth    │ accepts invites      │   Hearth    │   ▲ 127.0.0.1  │
 │ web UI         │◀────────────▶│ every log            │◀───────────▶│ web UI         │
 │ node           │  encrypted   │ every image chunk    │  encrypted  │ node           │
 │ log + feed     │   session    │ no web UI            │   session   │ log + feed     │
 └────────────────┘              └──────────────────────┘             └────────────────┘
```

Every member dials the home node, and members don't connect to each other. Both sides run identical code. The home node is a required, trusted hub: if it's down, nobody can sync or join until it's back.

The full design is in [`docs/architecture.md`](docs/architecture.md).

## Getting started

Python 3.12 or newer is required on every machine.

### Try both sides on one computer

The quickest way to see it working. In one terminal:

```
python -m venv .venv
.venv\Scripts\activate                 (macOS/Linux: source .venv/bin/activate)
pip install .
python -m hearth init --profile home
python -m hearth create-group --profile home
python -m hearth run --profile home --role home --listen 127.0.0.1:7777
```

In a second terminal, with the same virtualenv active:

```
python -m hearth invite --profile home --host 127.0.0.1:7777
python -m hearth join "<the link>" --profile friend --name Friend
python -m hearth run --profile friend --web-port 8081
```

Open both printed links. A post in one tab shows up in the other within about 5 seconds.

### Join a group (for friends)

You need an invite link from the group's owner, and a copy of this code. Then:

```
git clone https://github.com/iffayush/hearth && cd hearth
python -m venv .venv
.venv\Scripts\activate                 (macOS/Linux: source .venv/bin/activate)
pip install .
python -m hearth join "hearth://join?..." --profile me --name "Your Name"
python -m hearth run --profile me
```

`join` asks you to choose a passphrase. Nobody can reset it, so keep it safe. `run` asks for it each time and prints a link like `http://127.0.0.1:8080/?token=…`: open it to read and post, and keep `run` going while you use Hearth. Once you've joined, the last command is all you need.

### Run a home node (for the owner)

Use a Linux VM with systemd, and follow [`docs/deploy.md`](docs/deploy.md). It covers:

- choosing how friends reach the VM: port forwarding, a WireGuard VPS for carrier-grade NAT, or Tailscale Funnel;
- running Hearth in a VM of its own when the host sits on someone else's tailnet;
- the hardened service, the firewall, invites, nightly backups, restoring, and updates.

## Command reference

Every command takes `--profile NAME` (data in `~/.hearth/NAME/`) or `--data-dir PATH`. Commands that unlock keys also take `--passphrase-file PATH`, which must be mode `0600` on POSIX. Run any command with `--help` for its options.

| Command | What it does |
| --- | --- |
| `init` | Create an encrypted identity and `config.json`. `--kdf interactive` uses less memory on slow machines. |
| `create-group` | Start a group and its key, with this profile as admin. |
| `run` | Start the node until Ctrl+C. Options: `--listen`, `--dial`, `--dial-pubkey`, `--role home`, `--headless`, `--web-port`, `--proxy-protocol`, `--verbose`. |
| `invite` | (Home node) Make a single-use invite link. `--host` sets the address friends dial, `--expires` how long it lasts (default 48h). |
| `join "LINK"` | Join a group with an invite link. `--name` sets your display name. |
| `status` | Members, entries per author, forks, connections, uploads waiting, and storage against the quota. |
| `post "text"` | Add a post from the terminal. `--image FILE` attaches a photo, up to 4 times. |
| `feed` | Print the newest posts. `--limit N` sets how many. |
| `backup --out FILE` | Write a consistent `.tar` while the node runs. It never includes the passphrase. |
| `debug log` | List every stored entry. Needs no keys. |
| `debug verify` | Re-check every signature, hash chain and image chunk. |
| `debug image A S I --out F` | Save image `I` of post `S` by author `A`. |
| `debug bench-kdf` | Time Argon2id on this machine, to help choose `--kdf`. |

When a node is already running, one-shot commands send their request to it over a local control channel, so you don't have to stop it.

## Configuration and files

A node's data directory holds:

| Path | Contents |
| --- | --- |
| `identity.key` | Your two private keys, encrypted with your passphrase (Argon2id and SecretBox) |
| `group.key` | The group key, encrypted the same way |
| `hearth.db` | SQLite: entries, members, the outbox and invites. Content is ciphertext; metadata isn't. |
| `chunks/` | Encrypted image pieces |
| `config.json` | The settings below |
| `hearth.lock` | Enforces one process per data directory |
| `control.sock` or `control.token` | The local control channel (Unix socket on POSIX, token and loopback port on Windows) |

Settings in `config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `profile` | (required) | This node's profile name |
| `role` | `member` | `member` or `home`. `run --role home` saves it. |
| `listen` | `127.0.0.1:7777` | The address a home node listens on |
| `home_host` | none | The home node's address: `host:port` or `tls://host:port`. `join` saves it. |
| `home_pubkey` | none | The home node's pinned public key. `join` saves it. |
| `public_host` | none | The address invites use if `--host` is left out |
| `web_port` | `8080` | The web UI's port on `127.0.0.1` |
| `storage_quota_gib` | `20` | When the home node stops accepting new image chunks |

## Limits

These come from [`hearth/config.py`](hearth/config.py).

| Limit | Value |
| --- | --- |
| Post text | 10,000 characters |
| Images per post | 4 |
| Image size | 10 MiB |
| Log entry | 64 KiB |
| Wire frame | 2 MiB |
| Connections | 64 in total, 8 per IP address |
| Invite attempts | 5 per IP address per 10 minutes |
| Sync requests | 100 per minute per peer |
| Handshake timeout | 10 s |
| Idle timeout | 60 s |

## Documentation

| Document | Read it for |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | How the system fits together, from modules to wire protocol |
| [`docs/deploy.md`](docs/deploy.md) | Running a home node: reachability, install, backups, restore, what's been tested |
| [`SECURITY.md`](SECURITY.md) | Threat model, what is and isn't protected, how to report a problem |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup, code rules, how to test |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed in each release |
| [`PROGRESS.md`](PROGRESS.md) | Build status, open items, and the definition of done |
| [`docs/decisions.md`](docs/decisions.md) | Every design ambiguity resolved, with the date and the reasoning |
| [`docs/spec.md`](docs/spec.md) | The original build specification |
| [`docs/phase-1.md`](docs/phase-1.md) to [`docs/phase-8.md`](docs/phase-8.md) | A guided tour of each phase, with commands to try and experiments to run |

## Project layout

```
hearth/
  __main__.py, cli.py, cli_setup.py    command line
  web.py, static/                      local web UI
  actions.py, feed.py                  what a user can do; turning entries into posts
  node.py, control.py                  the running node; local control channel
  sync.py, fetcher.py, outbox.py       logs, image downloads, image uploads
  pending.py                           entries waiting for their author's membership
  peer.py, session.py, protocol.py     one connection: state machine, crypto, messages
  network.py, transport.py             addresses, backoff, TLS and PROXY v2
  store.py, tables.py, chunks.py       SQLite and image storage
  entry.py, canonical.py               signed log entries and strict JSON
  identity.py, keyfile.py, group.py    keys and passphrase-encrypted files
  crypto.py, invite.py                 group encryption; invite links
  audit.py, backup.py                  debug log and verify; backups
  settings.py, config.py, lockfile.py  settings, limits, one process per directory
deploy/hearth-home.service             the hardened systemd unit
docs/                                  see the table above
tests/                                 one test file per module, plus end-to-end tests
```

## Development

```
pip install -e ".[dev]"
python -m pytest
python -m bandit -q -r hearth
python -m pip_audit --skip-editable
```

CI runs the same checks on Linux, Windows and macOS. Some tests are POSIX-only, so Windows skips a few. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the code rules.

## Think you can do better?

Good. Hearth is a development project, and it's here to be picked apart. If you can see a better design or a hole in this one, do the work and open a pull request:

- **Found a vulnerability?** Send the fix, with a test that fails without it.
- **Know a better protocol?** Send the change and its tests, and explain what it improves and what happens to existing data. Start with [`docs/architecture.md`](docs/architecture.md) for how it works and [`docs/decisions.md`](docs/decisions.md) for why it works that way.
- **Want something else to do?** Pick anything from [Not built yet](#not-built-yet), or fix something in [`PROGRESS.md`](PROGRESS.md)'s open items.

"This looks insecure" with no code doesn't help much. A PR that proves it does. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to get set up.

## Not built yet

Designed for, but outside the first version: removing members and rotating the key, editing or deleting posts, changing a passphrase, comments, video, direct member-to-member connections, NAT hole punching, Android, Tor, more than one group per node, and storage cleanup.

## License

The code and documentation are under the [MIT License](LICENSE). It comes with no warranty, which matters for a development project with an unaudited protocol.

The Hearth logo and banner in `drawables/` aren't covered by it: all rights to them are reserved.

<p align="center">
  <br>
  <img src="drawables/logo.png" alt="" width="64"><br>
  <sub>Warm hands, cold keys.</sub>
</p>

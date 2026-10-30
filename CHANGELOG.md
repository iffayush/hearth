# Changelog

Notable changes to Hearth. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0, a stored format or the wire protocol may change between versions.

## [Unreleased]

### Added

- Project documentation: a detailed README, `docs/architecture.md`, `SECURITY.md`, `CONTRIBUTING.md` and this changelog.
- Repository setup: `.gitignore` rules for secrets and backups, `.gitattributes`, Dependabot, and issue and pull request templates.
- CI now also runs on macOS, and only on pushes to `main` and on pull requests.
- The Hearth banner and logo in `drawables/`, used in the README, and a 1280x640 image for GitHub's social preview.
- The MIT License for the code and documentation. The artwork in `drawables/` isn't covered.

### Fixed

- A `--data-dir` too long for the OS's Unix socket limit now gives a clear error instead of a traceback. macOS's limit is 104 bytes.

### Changed

- A connection that starts with HTTP or TLS is now named. Web crawlers log one INFO line instead of a `Frame too big` warning.
- An invite whose address is a web server, or needs `tls://`, now says so and asks for a corrected link.
- `debug log` with no group now names `create-group` and `join`.
- The build specification moved to `docs/spec.md`.
- The docs now say plainly that this is a development project, and invite protocol improvements and security fixes as pull requests.

## [0.1.0] - 2026-10-29

The first complete build, made in eight phases. Each has a guided tour in `docs/phase-N.md`.

### Added

- **Phase 1, identity and signed entries:** Ed25519 and X25519 identities, Argon2id key files, canonical JSON, and hash-chained signed entries.
- **Phase 2, storage:** SQLite in WAL mode, pure entry validation, fork detection, a lock per data directory, and `debug log` and `debug verify`.
- **Phase 3, two nodes talk:** framed protocol, authenticated handshake, encrypted per-direction sessions, keepalive, connection limits, and reconnect backoff.
- **Phase 4, sync:** `HEADS`, `WANT` and `ENTRIES`, relaying through a third node, a pending queue for entries that arrive before their author's membership, and the local control channel.
- **Phase 5, group encryption and images:** the group key, encrypted posts, encrypted image chunks, the upload outbox, on-demand fetching, and the storage quota.
- **Phase 6, membership and invites:** single-use invite links, limited sessions for strangers, and `status`.
- **Phase 7, web UI:** a local page on `127.0.0.1` with a per-launch token, a merged feed, posting and images.
- **Phase 8, home node and real network:**
  - `run --role home`, online `backup`, and `debug bench-kdf`;
  - a hardened systemd unit, and `docs/deploy.md`;
  - Tailscale Funnel support through `tls://` addresses and `run --proxy-protocol`.

[Unreleased]: #unreleased
[0.1.0]: #010---2026-10-29

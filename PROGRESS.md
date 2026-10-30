# Hearth build progress

Where the build stands, phase by phase. Updated at the end of every phase.
Detail lives in `docs/phase-N.md`; every resolved ambiguity is in `docs/decisions.md`.

## Phases

| # | Phase | Status | Tests | Doc |
| --- | --- | --- | --- | --- |
| 1 | Identity and signed entries | ✅ Done 2026-10-26 | 97 passed, 2 skipped (POSIX-only) | [phase-1](docs/phase-1.md) |
| 2 | Storage | ✅ Done 2026-10-26 | 127 passed, 2 skipped (POSIX-only) | [phase-2](docs/phase-2.md) |
| 3 | Two nodes talk | ✅ Done 2026-10-26 | 186 passed, 2 skipped (POSIX-only) | [phase-3](docs/phase-3.md) |
| 4 | Sync | ✅ Done 2026-10-26 | 234 passed, 2 skipped (POSIX-only) | [phase-4](docs/phase-4.md) |
| 5 | Group encryption and images | ✅ Done 2026-10-26 | Windows 338 passed, 2 skipped · Linux/3.12 340 passed | [phase-5](docs/phase-5.md) |
| 6 | Membership and invites | ✅ Done 2026-10-26 | Windows 389 passed, 2 skipped · Linux/3.12 391 passed | [phase-6](docs/phase-6.md) |
| 7 | Web UI | ✅ Done 2026-10-27 | Windows 439 passed, 2 skipped · Linux/3.12 441 passed | [phase-7](docs/phase-7.md) |
| 8 | Home node and real network | ✅ Built 2026-10-29; real-VM checks are the owner's | Windows 508 passed, 4 skipped · Linux/3.12 512 passed (2026-10-30) | [phase-8](docs/phase-8.md) |

## Moved between phases

- Connection limits (64 total, 8 per IP) and reconnect backoff were built in Phase 3, not later.
- `debug sign` (Phase 1) was removed in Phase 4; `post` replaces it.
- `debug create-group` moved from Phase 4 to Phase 2, so `debug log` has real entries to show (owner's choice, 2026-10-26).
- Invite retry after a lost INVITE_OK was dropped from Phase 6 (owner's choice, 2026-10-26): links are strictly single-use.
- Tailscale Funnel support was added to Phase 8 (owner's choice, 2026-10-29), because the VM is reachable only through Tailscale: `tls://` addresses and `run --proxy-protocol`, deploy.md's path C. Not in the spec; see decisions.md.

## Next up

All 8 phases are built. What's left needs the owner's VM and networks (`docs/deploy.md`, "Is it done?"), following path C (Tailscale Funnel):
1. `debug bench-kdf` on the VM; install the service; reboot, and it comes back with the funnel.
   - ✅ 2026-10-30: the home node runs in a Multipass VM (`hearth`) on its own VLAN on the owner's host. The VM is on the owner's own tailnet, tagged `tag:hearth`, and runs Funnel itself, because the host sits on someone else's tailnet. A laptop on a phone hotspot without Tailscale joined via `tls://hearth.<tailnet>.ts.net:443` and synced; the log showed its real IP.
   - ✅ 2026-10-30 (owner-reported): after the host rebooted, the VM, the service and the funnel all came back.
2. Two laptops on different networks, without Tailscale, join and sync a post with a photo.
3. One laptop offline for a day catches up.
4. A backup taken while the service runs restores, and `debug verify` passes.

Then:
- remove the first install from the host (the VM setup is in `docs/deploy.md`, "Path C in a VM of its own", since 2026-10-30);
- the MVP checklist below.

## Open items and known gaps

- The repository is public at github.com/iffayush/hearth since 2026-10-30. macOS was added to the CI list without ever having been run by hand. The same checks also run locally on Linux in Docker (see below).
- Outside pull requests are invited. Security problems arrive that way too, so they are public from the moment a PR opens.
- Phase 1's `debug verify FILE`, `--group` and `--out` were replaced in Phase 2 (see decisions.md).
- Path C was tested with HAProxy standing in for Tailscale Funnel (same job: TLS on 443, PROXY v2 inside). Real Funnel has worked for a join and sync since 2026-10-30; its unpublished bandwidth limits are still untested with photos. If photos crawl, path B (WireGuard VPS) is the fallback.
- The photo picker's previews were checked by tests of the upload API, not by hand in a browser: automation can't drive a native file chooser. Worth one manual try.
- A joiner whose INVITE_OK was lost is left as a key-less member (its `member_add` exists). Harmless, since it can't read anything, but it stays in the member list; removing members is out of scope for the MVP.

## Resolved on 2026-10-30

- Web crawlers that find the Funnel name now log one INFO line instead of `WARNING … Frame too big (1140.3 MiB > 2 MiB)`, which was "GET " read as a frame length. Confirmed on the real VM the same day.
- An invite whose address is a web server (say, the web UI's 8080) or needs `tls://` now says so and asks for a corrected link, instead of `Frame too big (1157.3 MiB > 2 MiB)` and "You can retry the same link".
- `debug log` with no group names `create-group` and `join`, not the old `debug create-group`.

## Resolved in Phase 8

- `run`'s stdout is line-buffered, so the banner and browser link reach a pipe or the journal at once.
- `run --role home` saves the role, and `invite --host` or `public_host` sets the address: no more hand-editing `config.json` to make a home node.
- `status` with no node running no longer crashes.

## Resolved on 2026-10-26

- The POSIX-only code (Unix-socket control channel, `fcntl` lock, 0600 permission checks) now runs and passes on Linux (Docker, Python 3.12).
- The passphrase prompt (`getpass`) has automated tests (in-process, prompt mocked).
- Connections closed for security reasons are logged at WARNING.
- Port 7001: the stray node seen during the Phase 4 demo is gone.
- The Linux run found a real bug the Windows run couldn't: a missing import that only crashes on Python 3.12. Fixed in Phase 5.

## How to check on Linux (what CI will do)

With Docker Desktop running, from the project folder in PowerShell (one line):

```
docker run --rm -v "${PWD}:/src:ro" python:3.12-slim sh -c "cp -r /src /work && cd /work && rm -rf .venv && pip install -q --upgrade pip && pip install -q -e '.[dev]' && python -m pytest -q && python -m bandit -q -r hearth && python -m pip_audit --skip-editable"
```

## Data-format resets

When a phase changes stored formats, old test data directories must be deleted (no migrations before the MVP).

| Phase | Delete old data dirs? | Why |
| --- | --- | --- |
| 1 | — | No stored data yet |
| 2 | No | Phase 1 dirs only hold `identity.key` and `config.json`; `hearth.db` is created on first use |
| 3 | No | Nothing on disk changed |
| 4 | No | Nothing on disk changed; posts are still plaintext until Phase 5 |
| 5 | **Yes** | Posts are encrypted boxes now; schema version 2 refuses older databases |
| 6 | No | Only an optional `public_host` was added to `config.json` |
| 7 | No | Nothing on disk changed |
| 8 | No | `config.json` may now hold `tls://` addresses and a saved `role`; older files still work |

## MVP definition of done

- [x] All tests pass; no test uses `time.sleep` for synchronisation
- [ ] CI green: `pytest`, `bandit`, `pip-audit` clean or findings justified
- [x] Home node running on the VM under systemd, surviving a reboot (2026-10-30: a Multipass VM, its host rebooted; owner-reported)
- [ ] Two laptops on different networks sync posts with images through it
- [x] A friend joins with only an invite link and sees full history (tests and containers; again on real networks)
- [x] An image survives its poster going offline right after posting (`test_image_survives_its_poster_going_offline`)
- [x] The SQLite file and chunk folder contain no readable content (`test_nothing_readable_is_stored`)
- [x] `docs/phase-1.md` to `docs/phase-8.md`, `docs/decisions.md` and `docs/deploy.md` written
- [x] README explains setup in under 10 commands (a friend's join: 6)

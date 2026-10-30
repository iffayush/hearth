"""The CLI's one-shot commands, and helpers shared by every command.

`__main__.py` parses arguments; this file and `cli_setup.py` run them. The CLI
is the only layer allowed to print().

A one-shot command (post, feed, invite, status, backup, debug ...) builds an
actions request and hands it to `one_shot()`, which either

- takes the data-dir lock and runs the action itself (no node running), or
- finds the lock held, so a node *is* running, and sends the request over the
  control channel for that node to run: one writer per log, no second
  passphrase prompt.

Commands that work on key files directly (init, run, group setup) are in
`cli_setup.py`.

Joke: a CLI is a conversation where one side only speaks in flags.
"""

import argparse
import asyncio
import base64
import getpass
import os
import stat
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from hearth import actions, backup, config, control, group, invite, keyfile, settings
from hearth.chunks import ChunkStore
from hearth.identity import Identity
from hearth.lockfile import DataDirLock, LockHeldError
from hearth.network import parse_dial_address
from hearth.store import Store


class CliError(Exception):
    """A user-facing error: printed as one line, exit code 1."""


# --- One-shot commands -----------------------------------------------------------------


def command_post(args: argparse.Namespace) -> int:
    if len(args.image) > config.MAX_IMAGES_PER_POST:
        raise CliError(f"at most {config.MAX_IMAGES_PER_POST} images per post")
    # Absolute paths, because a running node resolves them from its own working directory.
    images = [str(Path(path).resolve()) for path in args.image]
    result = one_shot(args, {"op": "post", "text": args.text, "images": images})
    if result["waiting"]:
        print(f"Waiting to upload: {result['images']} image(s) in {result['chunks']} chunk(s). The post appears "
              "once the home node has every chunk (keep or start your node so it can upload).")
    else:
        print(f"Posted as #{result['seq']} (hash {result['hash'][:12]}…).")
    return 0


def command_feed(args: argparse.Namespace) -> int:
    result = one_shot(args, {"op": "feed", "limit": args.limit})
    for item in result["waiting"]:
        print(f"[{format_time(item['created'])}] (you, waiting to upload: "
              f"{item['uploaded']}/{item['chunks']} chunks)  {item['text']}")
    if not result["posts"] and not result["waiting"]:
        print("No posts yet. The fire's lit, but nobody's said anything.")
    for item in result["posts"]:
        if item["problem"] is not None:
            text = f"(unreadable: {item['problem']})"
        else:
            text = item["text"] + (f"  [{item['images']} image(s): debug image {item['author']} "
                                   f"{item['seq']} 0 --out pic]" if item["images"] else "")
        who = item["name"] or item["author"][:8]
        print(f"[{format_time(item['sort_ts'])}] {who} #{item['seq']}  {text}")
    return 0


def command_image(args: argparse.Namespace) -> int:
    request = {"op": "image", "author": args.author, "seq": args.seq, "index": args.index}
    result = one_shot(args, request)
    args.out.write_bytes(base64.b64decode(result["data"]))
    print(f"Saved {result['mime']} ({result['size']} bytes) to {args.out}; every chunk and the whole image verified.")
    return 0


def command_invite(args: argparse.Namespace) -> int:
    try:
        expires = invite.parse_duration(args.expires)
        host_port = args.host or settings.load_settings(existing_data_dir(args)).public_host
        if host_port is None:
            raise CliError("say how friends reach this node: --host name.example.net:7777, "
                           "or set \"public_host\" in config.json")
        home = parse_dial_address(host_port)
    except (invite.InviteError, ValueError) as error:
        raise CliError(str(error)) from error
    result = one_shot(args, {"op": "invite", "host": home.host, "port": home.port, "tls": home.tls,
                             "expires": expires})
    print("Send this link to ONE friend, privately. It works once, and expires", format_time(result["expires"]))
    print(result["link"])
    if home.host.replace(".", "").isdigit():
        print("(That's a raw IP address. For real use, prefer a hostname, e.g. dynamic DNS, so links survive IP changes.)")
    return 0


def command_status(args: argparse.Namespace) -> int:
    result = one_shot(args, {"op": "status"})
    if result["group"] is None:
        print("Not in a group yet: run `create-group`, or `join` with an invite link.")
        return 0
    role = "the home node and admin" if result["is_home"] else "a member"
    print(f"group {result['group'][:8]}…  admin {result['admin'][:8]}…  you {result['me'][:8]}…, {role}")
    print(f"members: {result['members']}   waiting to upload: {result['outbox']} post(s)")
    used = result["storage_used"] / 2**20
    quota = f" of {result['storage_quota'] / 2**30:.0f} GiB quota" if result["is_home"] and result["storage_quota"] else ""
    print(f"image storage: {used:.1f} MiB{quota}")
    for author in result["forked"]:
        print(f"WARNING: {author[:8]} is forked (two copies of one identity are writing)")
    print("logs:")
    for item in result["authors"]:
        noun = "entry" if item["entries"] == 1 else "entries"
        print(f"  {item['author'][:8]}  {item['name'] or '(no name yet)':<20} {item['entries']} {noun}")
    if result["connections"] is None:
        print("connections: none (no node running for this profile)")
    else:
        print(f"connections: {len(result['connections'])}")
        for conn in result["connections"]:
            print(f"  {conn['pubkey'][:8] or '?':<8}  {conn['address']:<21} {conn['state']}")
    return 0


def command_log(args: argparse.Namespace) -> int:
    result = one_shot(args, {"op": "log"})
    if result["group"] is None:
        print("No group yet, so no entries. Run `create-group` to light the fire, or `join` with an invite link.")
        return 0
    print(f"group {result['group'][:8]}…  admin {result['admin'][:8]}…  members {result['members']}")
    for author in result["forked"]:
        print(f"WARNING: {author[:8]} is forked (two copies of one identity are writing)")
    for item in result["entries"]:
        print(f"{item['author'][:8]}  #{item['seq']:<4} {item['kind']:<12} "
              f"{format_time(item['sort_ts'])}  {item['hash'][:12]}  {item['summary']}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    result = one_shot(args, {"op": "verify"})
    chunks = result["chunks"]
    for problem in result["problems"]:
        print(f"BAD {problem['author'][:8]} #{problem['seq']} ({problem['code']}): {problem['message']}")
    for problem in chunks["problems"] if chunks is not None else []:
        print(f"BAD image chunk {problem['chunk'][:12]}…: {problem['problem']}")
    if result["problems"]:
        print(f"{len(result['problems'])} of {result['checked']} entries failed verification.")
    else:
        print(f"All {result['checked']} entries verified: signatures good, chains unbroken.")
    if chunks is None:
        print("Image chunks weren't checked: that can take minutes, so stop the node to include them.")
    elif chunks["problems"]:
        print(f"{len(chunks['problems'])} of {chunks['checked']} image chunks are missing or damaged.")
    else:
        print(f"All {chunks['checked']} image chunks match their hashes.")
    return 1 if result["problems"] or (chunks is not None and chunks["problems"]) else 0


def command_backup(args: argparse.Namespace) -> int:
    data_dir = existing_data_dir(args)
    out = args.out.resolve()
    # Checked before the snapshot too, so a typo costs nothing.
    if out.exists():
        raise CliError(f"{out} already exists; pick a new name (say, with today's date in it)")
    result = one_shot(args, {"op": "backup"})
    if not backup.is_snapshot_name(result["snapshot"]):
        raise CliError("the node's database snapshot has a strange name; nothing was written")
    snapshot = data_dir / result["snapshot"]
    try:
        written = backup.write_archive(data_dir, snapshot, out)
    finally:
        snapshot.unlink(missing_ok=True)
    print(f"Backed up {plural(result['entries'], 'entry', 'entries')} and "
          f"{plural(written['chunks'], 'image chunk', 'image chunks')} ({written['bytes'] / 2**20:.1f} MiB) to {out}")
    print("The passphrase is not in it, on purpose: keep it somewhere else, like your password manager.")
    return 0


def command_bench_kdf(args: argparse.Namespace) -> int:
    print("Timing Argon2id, the passphrase lock on identity.key and group.key. `run` unlocks both,")
    print(f"so a start costs two of these. Each is timed {plural(args.rounds, 'time', 'times')}; the median shows:")
    for kdf in ("interactive", "moderate"):
        seconds = statistics.median(keyfile.time_kdf(kdf) for _ in range(args.rounds))
        memory = keyfile.KDF_PARAMS[kdf][1] // 2**20
        print(f"  {kdf:<12} {memory:>4} MiB  {seconds:5.2f} s per key file, about {2 * seconds:.1f} s per start")
    print("Keep the default (moderate) unless a start takes too long here, or this machine can't spare")
    print("256 MiB while it starts. To switch, create the profile with `init --kdf interactive`.")
    return 0


# --- Running a request here or on the running node ----------------------------------


def one_shot(args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    """Run `request` here if no node is running on the data dir, else on the running node."""
    data_dir = existing_data_dir(args)
    lock = DataDirLock(data_dir)
    try:
        lock.acquire()
    except LockHeldError:
        # A node owns this data dir. Ask it, so there's one writer and no second passphrase prompt.
        return asyncio.run(control.send_request(data_dir, request))
    try:
        identity, group_key = load_keys_for(request["op"], data_dir, args)
        role = settings.load_settings(data_dir).role
        with Store.open(data_dir / config.DATABASE_FILE) as store:
            context = actions.Context(store, chunk_store(data_dir, store), identity, group_key, role == "home",
                                      data_dir=data_dir)
            return asyncio.run(actions.perform(context, request))
    finally:
        lock.release()


def load_keys_for(op: str, data_dir: Path, args: argparse.Namespace) -> tuple[Identity | None, bytes | None]:
    """Load only the keys this op needs, asking for the passphrase at most once."""
    needs_identity = op in actions.NEEDS_IDENTITY
    needs_group_key = op in actions.NEEDS_GROUP_KEY
    if not needs_identity and not needs_group_key:
        return None, None
    passphrase = read_passphrase(args, confirm=False)
    identity = Identity.load(data_dir / config.IDENTITY_FILE, passphrase) if needs_identity else None
    group_key = group.load_group_key(data_dir, passphrase) if needs_group_key else None
    return identity, group_key


def chunk_store(data_dir: Path, store: Store) -> ChunkStore:
    root = data_dir / config.CHUNKS_DIR
    root.mkdir(mode=0o700, exist_ok=True)
    return ChunkStore(root, store)


# --- Helpers ---------------------------------------------------------------------------


def resolve_data_dir(args: argparse.Namespace) -> Path:
    if args.data_dir is not None:
        return args.data_dir
    if args.profile is None:
        raise CliError("say which node: pass --profile NAME or --data-dir PATH")
    if not settings.is_valid_profile(args.profile):
        raise CliError(f"{args.profile!r} isn't a valid profile name (letters, digits, _ and -)")
    return config.DEFAULT_DATA_ROOT / args.profile


def existing_data_dir(args: argparse.Namespace) -> Path:
    """The data directory for a command that needs `init` to have run already."""
    data_dir = resolve_data_dir(args)
    if not (data_dir / config.IDENTITY_FILE).exists():
        raise CliError(f"no identity in {data_dir}; run `python -m hearth init` first")
    # On a server, `sudo hearth ...` would leave root-owned files the service can't open.
    if os.name == "posix" and data_dir.stat().st_uid != os.geteuid():
        raise CliError(f"{data_dir} belongs to another user; run this as that user "
                       f"(on the home node: sudo -u hearth ...)")
    return data_dir


def read_passphrase(args: argparse.Namespace, confirm: bool) -> str:
    """Get the passphrase from --passphrase-file, or prompt (twice when creating keys)."""
    passphrase_file = getattr(args, "passphrase_file", None)
    if passphrase_file is not None:
        passphrase = read_passphrase_file(passphrase_file)
    else:
        passphrase = getpass.getpass("Passphrase: ")
        if confirm and getpass.getpass("Same passphrase again: ") != passphrase:
            raise CliError("the two passphrases don't match")
    if not passphrase:
        raise CliError("the passphrase can't be empty")
    return passphrase


def read_passphrase_file(path: Path) -> str:
    """Read a passphrase file, refusing on POSIX if group or others can read it."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise CliError(f"passphrase file {path} not found") from error
    # Windows has no POSIX permission bits; protection there comes from the
    # user-profile ACLs (see docs/decisions.md).
    if os.name == "posix" and stat.S_IMODE(mode) & 0o077:
        raise CliError(f"{path} is readable by other users; run `chmod 600 {path}` first")
    text = path.read_text(encoding="utf-8")
    # Editors add a trailing newline; it's not part of the passphrase.
    return text.removesuffix("\n").removesuffix("\r")


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def format_time(unix_ms: int) -> str:
    return datetime.fromtimestamp(unix_ms // 1000).strftime("%Y-%m-%d %H:%M:%S")

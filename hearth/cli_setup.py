"""CLI commands that work on a data dir directly: `init`, `run`, and group setup.

They refuse while a node is running on the same data dir, because they handle
key files and the passphrase, which a running node never keeps around:

- `init` creates the identity; `run` starts the node and keeps it running;
- `create-group` makes a group, its key, and the group key file;
- `join LINK` makes (or reuses) an identity, redeems the invite with the
  home node, fetches the group's history, and writes the member's profile.

The one-shot commands (post, feed, ...) are in cli.py.

Joke: setup is the bit with the forms. There are always forms.
"""

import argparse
import asyncio
import ipaddress
import logging
import os
import random
import signal
from collections.abc import Callable
from pathlib import Path

from hearth import actions, config, crypto, group, invite, settings
from hearth.cli import CliError, chunk_store, existing_data_dir, read_passphrase, resolve_data_dir
from hearth.identity import Identity
from hearth.lockfile import DataDirLock, LockHeldError
from hearth.network import DialTarget, parse_dial_address, parse_listen_address
from hearth.node import Node
from hearth.peer import CLOSED
from hearth.store import Store
from hearth.web import start_web


BANNER = r"""
        (
     )   )   (
    (   (  )  )      H E A R T H
     ) _)_(_ (       a private forum that lives on your friends' laptops
    .-'-----'-.
   /___________\
"""


# --- init and run ------------------------------------------------------------------


def command_init(args: argparse.Namespace) -> int:
    data_dir = resolve_data_dir(args)
    profile = args.profile or data_dir.name
    if not settings.is_valid_profile(profile):
        raise CliError(f"{profile!r} isn't a valid profile name; pass --profile (letters, digits, _ and -)")
    # 0700: the folder holds key files and the control channel.
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with DataDirLock(data_dir):
        identity_path = data_dir / config.IDENTITY_FILE
        if identity_path.exists():
            raise CliError(f"{identity_path} already exists. One identity per profile; it's not a hydra.")
        passphrase = read_passphrase(args, confirm=True)
        identity = Identity.create(identity_path, passphrase, kdf=args.kdf)
        settings.save_settings(data_dir, settings.NodeSettings(profile=profile))
    print(f"Created identity for profile {profile!r} in {data_dir}")
    print(f"Your account ID: {identity.pubkey}")
    print("Keep your passphrase safe: there is no 'forgot password' link in a P2P forum.")
    return 0


def command_run(args: argparse.Namespace) -> int:
    data_dir = existing_data_dir(args)
    if args.verbose:
        # Only Hearth's own loggers; asyncio's debug chatter helps nobody here.
        logging.getLogger("hearth").setLevel(logging.DEBUG)
    with DataDirLock(data_dir):
        node_settings = settings.load_settings(data_dir)
        if args.role is not None:
            node_settings.role = args.role
        listen, dial = network_plan(args, node_settings)
        passphrase = read_passphrase(args, confirm=False)
        identity = Identity.load(data_dir / config.IDENTITY_FILE, passphrase)
        group_key = group.load_group_key(data_dir, passphrase)
        print_welcome(identity, node_settings.profile, listen, dial)
        if args.proxy_protocol:
            print("Every caller's address comes from the front door's PROXY v2 header (--proxy-protocol).")
        if group_key is None:
            print("No group key yet: syncing works, but posting and reading need `create-group`, or `join` with an invite link.")
        with Store.open(data_dir / config.DATABASE_FILE) as store:
            remember_role(data_dir, node_settings, store, identity)
            node = Node(
                identity, store, chunk_store(data_dir, store), group_key=group_key,
                is_home=node_settings.role == "home",
                storage_quota=node_settings.storage_quota_gib * 2**30,
                listen=listen, proxy_protocol=args.proxy_protocol, dial=[dial] if dial else [], control_dir=data_dir,
            )
            web_port = None if args.headless else (args.web_port or node_settings.web_port)
            try:
                asyncio.run(run_until_stopped(node, web_port))
            except KeyboardInterrupt:
                pass
            except OSError as error:
                # Almost always a port: another node (or program) already has it.
                raise CliError(f"couldn't start: {error.strerror or error}. Is another node already "
                               "using that port? (--listen, or --web-port for the web UI)") from error
    print("\nNode stopped. The fire's out; the embers are safe on disk.")
    return 0


def remember_role(data_dir: Path, node_settings: settings.NodeSettings, store: Store, identity: Identity) -> None:
    """Check the role fits this profile, then keep it in config.json so one-shot commands agree."""
    admin = store.admin()
    if node_settings.role == "home" and admin is not None and admin != identity.pubkey:
        raise CliError("only the group's admin can be its home node, and this profile is a member; "
                       "run without --role home")
    if settings.load_settings(data_dir).role != node_settings.role:
        settings.save_settings(data_dir, node_settings)
        print(f"Saved role {node_settings.role!r} in config.json, so other commands know it too.")


async def run_until_stopped(node: Node, web_port: int | None) -> None:
    """Run the node (and its web UI, unless headless) until Ctrl+C, or SIGTERM from systemd."""
    stopping = asyncio.Event()
    if os.name == "posix":
        # systemd stops a service with SIGTERM. Treat it like Ctrl+C, so the finally
        # blocks below close connections and remove the control socket.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    await node.start()
    server = None
    try:
        if web_port is not None:
            server = await start_web(node, web_port)
            print(f"\nOpen Hearth in your browser:\n  {server.url()}\n"
                  "(That link includes this session's secret token: don't share it.)\n")
        await stopping.wait()
    finally:
        if server is not None:
            await server.close()
        await node.stop()


def print_welcome(
    identity: Identity, profile: str, listen: tuple[str, int] | None, dial: DialTarget | None
) -> None:
    print(BANNER)
    # Not security: just picking a tagline.
    print(f"  {random.choice(config.TAGLINES)}\n")  # nosec B311
    print(f"You are {identity.short_id}… (profile {profile!r}). Ctrl+C to stop.")
    if listen is None and dial is None:
        print("Not listening and nothing to dial: this node will sit quietly by the fire alone.")
    if dial is not None and dial.pubkey is None:
        print("Dialling without a pinned pubkey: fine for testing on one laptop, not for a real home node.")


def network_plan(
    args: argparse.Namespace, node_settings: settings.NodeSettings
) -> tuple[tuple[str, int] | None, DialTarget | None]:
    """Where to listen and whom to dial: CLI flags first, then config.json."""
    if args.dial_pubkey is not None and args.dial is None:
        raise CliError("--dial-pubkey only makes sense together with --dial")
    if args.dial_pubkey is not None and not actions.is_hex64(args.dial_pubkey):
        raise CliError("--dial-pubkey must be 64 lowercase hex characters")
    try:
        if args.listen is not None:
            listen = parse_listen_address(args.listen)
        elif node_settings.role == "home":
            listen = parse_listen_address(node_settings.listen)
        else:
            # Member nodes don't listen on the network in the MVP.
            listen = None
        dial = None
        if args.dial is not None:
            dial = parse_dial_address(args.dial, args.dial_pubkey)
        elif node_settings.home_host is not None:
            dial = parse_dial_address(node_settings.home_host, node_settings.home_pubkey)
    except ValueError as error:
        raise CliError(str(error)) from error
    if args.proxy_protocol and (listen is None or not ipaddress.IPv4Address(listen[0]).is_loopback):
        # Anyone who can reach the port could claim to be anyone, dodging the per-IP limits.
        raise CliError("--proxy-protocol believes whatever a PROXY header says, so it needs a loopback "
                       "address only the front door can reach: --listen 127.0.0.1:7777")
    return listen, dial



# --- Creating a group ---------------------------------------------------------------


def command_create_group(args: argparse.Namespace) -> int:
    data_dir = existing_data_dir(args)
    with OfflineLock(data_dir):
        passphrase = read_passphrase(args, confirm=False)
        identity = Identity.load(data_dir / config.IDENTITY_FILE, passphrase)
        if group.group_key_path(data_dir).exists():
            raise CliError("this profile already has a group key; one group per node in the MVP")
        with Store.open(data_dir / config.DATABASE_FILE) as store:
            # Key file first: a group whose key was never saved would be unreadable forever.
            group.save_group_key(data_dir, crypto.new_group_key(), passphrase)
            try:
                result = actions.create_group(store, identity)
            except actions.ActionError:
                group.group_key_path(data_dir).unlink()
                raise
    print(f"Created group {result['group']}")
    print(f"Admin (that's you): {result['admin']}")
    print("Next: run this profile as the home node (`run --role home`), then make invites with `invite`.")
    return 0


class OfflineLock:
    """The data-dir lock for commands that must not run alongside a node: `with OfflineLock(dir):`."""

    def __init__(self, data_dir: Path) -> None:
        self._lock = DataDirLock(data_dir)

    def __enter__(self) -> None:
        try:
            self._lock.acquire()
        except LockHeldError as error:
            raise CliError("stop this profile's running node first: this command handles key files") from error

    def __exit__(self, *exc_info: object) -> None:
        self._lock.release()


# --- Joining with an invite -----------------------------------------------------------


def command_join(args: argparse.Namespace) -> int:
    try:
        link = invite.parse_link(args.link)
    except invite.InviteError as error:
        raise CliError(str(error)) from error
    data_dir = resolve_data_dir(args)
    profile = args.profile or data_dir.name
    if not settings.is_valid_profile(profile):
        raise CliError(f"{profile!r} isn't a valid profile name; pass --profile (letters, digits, _ and -)")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with OfflineLock(data_dir):
        identity, passphrase = identity_for_joining(data_dir, args, profile)
        if group.group_key_path(data_dir).exists():
            raise CliError("this profile already belongs to a group; join with a new --profile")
        node_settings = settings.load_settings(data_dir)
        # From now on `run` dials the home node from the link, pinned to its key.
        node_settings.home_host, node_settings.home_pubkey = link.address(), link.home_pubkey
        settings.save_settings(data_dir, node_settings)
        with Store.open(data_dir / config.DATABASE_FILE) as store:
            if store.group_id() is not None:
                raise CliError("this profile already belongs to a group; join with a new --profile")
            node = Node(identity, store, chunk_store(data_dir, store), listen=None, dial=[])
            name = args.name or profile
            summary = asyncio.run(join_and_sync(node, link, name,
                                                lambda key: group.save_group_key(data_dir, key, passphrase)))
    print(f"Welcome to the group, {name}! {summary}")
    print(f"Keep in touch with: python -m hearth run --profile {profile}")
    return 0


def identity_for_joining(data_dir: Path, args: argparse.Namespace, profile: str) -> tuple[Identity, str]:
    """A fresh identity for a new profile, or the existing one if `init` already ran."""
    identity_path = data_dir / config.IDENTITY_FILE
    if identity_path.exists():
        passphrase = read_passphrase(args, confirm=False)
        return Identity.load(identity_path, passphrase), passphrase
    passphrase = read_passphrase(args, confirm=True)
    identity = Identity.create(identity_path, passphrase, kdf=args.kdf)
    settings.save_settings(data_dir, settings.NodeSettings(profile=profile))
    print(f"Created your identity: {identity.pubkey}")
    return identity, passphrase


async def join_and_sync(node: Node, link: invite.InviteLink, name: str, save_key: Callable[[bytes], None]) -> str:
    """Redeem the invite, fetch the group's history, then write our profile entry."""
    await node.start()
    try:
        try:
            peer = await node.join(link, save_key)
        except (OSError, TimeoutError) as error:
            raise CliError(f"couldn't reach the home node at {link.address()}: {error or 'timed out'}") from error
        await wait_until(lambda: peer.became_active.is_set() or peer.state == CLOSED, config.JOIN_TIMEOUT * 2)
        if not peer.became_active.is_set():
            if peer.error_received == "bad_invite":
                raise CliError(f"the invite didn't work: {peer.close_reason}. Ask the admin for a new link.")
            if peer.not_hearth:
                raise CliError(f"the join didn't work: {peer.close_reason}. Ask the admin for a corrected link.")
            raise CliError(f"the join didn't work: {peer.close_reason or 'no answer'}. You can retry the same link.")
        me = node.identity.pubkey
        # The profile waits until our own member_add has arrived, so it validates like anyone's.
        synced = await wait_until(lambda: me in node.store.members() and node.syncer.caught_up(peer),
                                  config.JOIN_SYNC_TIMEOUT)
        if not synced:
            return "You're in, but the history is still arriving; `run` will finish fetching it."
        if node.group_key is None:
            raise CliError("joined, but no group key was saved")
        profile = actions.set_profile(node.store, node.identity, node.group_key, name)
        await node.syncer.announce()
        await wait_until(lambda: node.syncer.has_served(peer, me, profile["seq"]), config.JOIN_SYNC_TIMEOUT)
        return f"You now hold the group's {node.store.entry_count()} entries."
    finally:
        await node.stop()


async def wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    """Poll `condition` until it's true (True) or `timeout` seconds pass (False)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(config.JOIN_POLL_INTERVAL)
    return True

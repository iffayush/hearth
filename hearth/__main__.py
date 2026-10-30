"""Command-line entry point: `python -m hearth <command>`.

This file only parses arguments and turns errors into one friendly line; the
commands themselves live in `cli_setup.py` (init, run, group setup) and
`cli.py` (everything else).

Commands:
    init --profile NAME       create an encrypted identity and config.json
    run [--listen H:P] [--dial H:P [--dial-pubkey HEX]] [--role home] [--headless]
        [--proxy-protocol]    start a node and keep it running until Ctrl+C
    create-group              start a group (and its key) with this profile as admin
    invite [--host H:P]       (home node) make a single-use invite link
    join "LINK" [--name N]    join a group with an invite link
    status                    members, logs, forks, storage, connections
    post "text" [--image F]   add an encrypted post, with up to 4 images
    feed [--limit N]          show the newest posts from everyone
    backup --out FILE         a consistent .tar of this node (never the passphrase)
    debug image A S I --out F save image I of post S by author A
    debug log                 list every stored entry (no keys needed)
    debug verify              re-check every signature, hash chain and image chunk
    debug bench-kdf           time Argon2id on this machine

Joke: argparse: because "please type it correctly" never worked.
"""

# HALL_OF_FAME: friends who joined the hearth. Add yourself when you do.
#   - (the owner, warming their hands)

import argparse
import logging
import sys
from pathlib import Path

from hearth import actions, backup, cli, cli_setup, config, control, keyfile, settings
from hearth.lockfile import LockHeldError
from hearth.store import StoreError

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
USER_ERRORS = (
    cli.CliError, keyfile.KeyFileError, LockHeldError, settings.SettingsError,
    StoreError, actions.ActionError, control.ControlError, backup.BackupError,
)


def main(argv: list[str] | None = None) -> int:
    """Run one command and return the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    # Post text can be any Unicode; don't let a Windows code page mangle it. Line
    # buffering: when output goes to a file or systemd's journal rather than a
    # terminal, `run`'s banner and browser link still show up at once.
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except USER_ERRORS as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; nothing was half-written.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hearth", description="Hearth: a private P2P forum.")
    commands = parser.add_subparsers(title="commands")

    init = commands.add_parser("init", help="create a new identity for a profile")
    add_location_arguments(init)
    init.add_argument("--kdf", choices=config.KDF_CHOICES, default=config.DEFAULT_KDF,
                      help="Argon2id strength for the key file (default: moderate)")
    init.set_defaults(handler=cli_setup.command_init)

    run = commands.add_parser("run", help="start this profile's node (Ctrl+C to stop)")
    add_location_arguments(run)
    run.add_argument("--listen", help="accept connections on this IPv4 host:port, e.g. 127.0.0.1:7001")
    run.add_argument("--dial", help="keep a connection to this host:port (tls://host:port to go inside TLS)")
    run.add_argument("--dial-pubkey", help="pin the dialled node's pubkey (64 hex); anyone else is refused")
    run.add_argument("--verbose", action="store_true", help="also log every PING and PONG")
    run.add_argument("--headless", action="store_true", help="don't start the web UI (the home node's way)")
    run.add_argument("--web-port", type=int, help="the web UI's port on 127.0.0.1 (default: web_port in config.json)")
    run.add_argument("--role", choices=settings.ROLES,
                     help="run as the group's home node (admin only) or a member; saved in config.json")
    run.add_argument("--proxy-protocol", action="store_true",
                     help="(home node behind Tailscale Funnel) read each caller's address from a PROXY v2 "
                          "header; needs --listen 127.0.0.1:PORT")
    run.set_defaults(handler=cli_setup.command_run)

    create_group = commands.add_parser("create-group", help="start a group, and its key, with you as admin")
    add_location_arguments(create_group)
    create_group.set_defaults(handler=cli_setup.command_create_group)

    invite_cmd = commands.add_parser("invite", help="(home node) make a single-use invite link")
    invite_cmd.add_argument("--expires", default="48h", help="how long it stays valid: 30m, 48h, 7d... (default 48h)")
    invite_cmd.add_argument("--host", help="host:port friends dial, or tls://host:port behind Tailscale Funnel "
                                           "(default: public_host in config.json)")
    add_location_arguments(invite_cmd)
    invite_cmd.set_defaults(handler=cli.command_invite)

    join = commands.add_parser("join", help="join a group with an invite link")
    join.add_argument("link", help="the hearth://join?... link, in quotes")
    join.add_argument("--name", help="your display name (default: the profile name)")
    join.add_argument("--kdf", choices=config.KDF_CHOICES, default=config.DEFAULT_KDF,
                      help="Argon2id strength for a new identity's key file (default: moderate)")
    add_location_arguments(join)
    join.set_defaults(handler=cli_setup.command_join)

    status = commands.add_parser("status", help="members, logs, forks, storage, connections")
    add_location_arguments(status)
    status.set_defaults(handler=cli.command_status)

    post = commands.add_parser("post", help="add an encrypted post to your log")
    post.add_argument("text", help="what you want to say (may be \"\" if you attach an image)")
    post.add_argument("--image", action="append", default=[], type=Path, metavar="FILE",
                      help=f"attach a JPEG, PNG, WebP or GIF (repeat for up to {config.MAX_IMAGES_PER_POST})")
    add_location_arguments(post)
    post.set_defaults(handler=cli.command_post)

    feed = commands.add_parser("feed", help="show the newest posts")
    feed.add_argument("--limit", type=int, default=actions.FEED_DEFAULT_LIMIT,
                      help=f"how many posts (1-{actions.FEED_MAX_LIMIT})")
    add_location_arguments(feed)
    feed.set_defaults(handler=cli.command_feed)

    backup_cmd = commands.add_parser("backup", help="write a consistent .tar backup (safe while the node runs)")
    backup_cmd.add_argument("--out", type=Path, required=True, help="the .tar file to create (must not exist yet)")
    add_location_arguments(backup_cmd, with_passphrase=False)
    backup_cmd.set_defaults(handler=cli.command_backup)

    add_debug_commands(commands)
    return parser


def add_debug_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    debug = commands.add_parser("debug", help="developer tools")
    debug_commands = debug.add_subparsers(title="debug commands")

    image = debug_commands.add_parser("image", help="save one image from a post to a file")
    image.add_argument("author", help="the post author's pubkey, 64 hex")
    image.add_argument("seq", type=int, help="the post's number in its author's log")
    image.add_argument("index", type=int, help="which image of the post, from 0")
    image.add_argument("--out", type=Path, required=True, help="where to save it")
    add_location_arguments(image)
    image.set_defaults(handler=cli.command_image)

    log = debug_commands.add_parser("log", help="list every stored entry")
    add_location_arguments(log, with_passphrase=False)
    log.set_defaults(handler=cli.command_log)

    verify = debug_commands.add_parser("verify", help="re-check every signature, hash chain and image chunk")
    add_location_arguments(verify, with_passphrase=False)
    verify.set_defaults(handler=cli.command_verify)

    bench = debug_commands.add_parser("bench-kdf", help="time Argon2id here, to choose --kdf for a slow machine")
    bench.add_argument("--rounds", type=int, default=3, choices=range(1, 11), metavar="N",
                       help="tries per setting, 1-10 (default 3)")
    bench.set_defaults(handler=cli.command_bench_kdf)


def add_location_arguments(parser: argparse.ArgumentParser, with_passphrase: bool = True) -> None:
    """--profile / --data-dir, plus --passphrase-file for commands that may open keys."""
    parser.add_argument("--profile", help="profile name; data lives in ~/.hearth/<profile>/")
    parser.add_argument("--data-dir", type=Path, help="use this data directory instead")
    if with_passphrase:
        parser.add_argument("--passphrase-file", type=Path,
                            help="read the passphrase from this file (must be mode 0600 on POSIX)")


if __name__ == "__main__":
    sys.exit(main())

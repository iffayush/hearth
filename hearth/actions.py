"""The things a user can ask their node to do: post, read the feed, look at an image...

Each action works on a `Context` (the store, the chunk store, and whichever
keys are loaded) and returns a JSON-friendly dict. There are two doorways to
the same code:

- no node running: the CLI takes the data-dir lock, loads what it needs, and
  calls `perform()` itself;
- node running: the CLI sends the request over the control channel
  (`control.py`) and the running node calls `perform()` with its own context.

A request is a dict like {"op": "post", "text": "hi", "images": []}.
`perform()` checks it against REQUESTS first, because over the control channel
it comes from outside the process.

Group setup (create-group, join-group) isn't here as a request: it writes key
files with the passphrase, so the CLI does it directly, with no node running.
`create_group()` and `join_group()` below are its database half.

Joke: one kitchen, two serving hatches. The soup tastes the same either way.
"""

import base64
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hearth import audit, backup, canonical, config, entry, feed, invite
from hearth.chunks import ChunkStore, ImageError, split_image
from hearth.entry import Reason
from hearth.fetcher import ChunkFetcher, FetchError, load_image
from hearth.identity import Identity
from hearth.outbox import Outbox
from hearth.store import ADDED, Store, StoreError

FEED_DEFAULT_LIMIT = 20
FEED_MAX_LIMIT = 100
MAX_PATH_CHARS = 4096


class ActionError(Exception):
    """The request can't be done. The message is shown to the user as-is."""


@dataclass
class Context:
    """Everything an action may use. Keys are None when they aren't loaded."""

    store: Store
    chunks: ChunkStore
    identity: Identity | None = None
    group_key: bytes | None = None
    # The home node keeps image chunks itself, so its posts skip the outbox.
    is_home: bool = False
    # Only a running node has one; without it, images come from disk only.
    fetcher: ChunkFetcher | None = None
    # A running node's live connections, for `status` (None: no node running).
    connections: list[dict[str, str]] | None = None
    storage_quota: int | None = None
    # Where `backup` puts its database snapshot (None: tests without a data dir).
    data_dir: Path | None = None


def is_hex64(value: Any) -> bool:
    return entry.is_hex64(value)


def is_post_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= config.MAX_POST_TEXT_CHARS


def is_image_paths(value: Any) -> bool:
    """Up to 4 absolute file paths (the CLI resolves them before sending)."""
    return (
        isinstance(value, list)
        and len(value) <= config.MAX_IMAGES_PER_POST
        and all(isinstance(path, str) and 0 < len(path) <= MAX_PATH_CHARS and Path(path).is_absolute()
                for path in value)
    )


def is_feed_limit(value: Any) -> bool:
    return canonical.is_int(value) and 1 <= value <= FEED_MAX_LIMIT


def is_seq(value: Any) -> bool:
    return canonical.is_int(value) and 1 <= value <= config.MAX_SEQ


def is_image_index(value: Any) -> bool:
    return canonical.is_int(value) and 0 <= value < config.MAX_IMAGES_PER_POST


def is_host(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 253 and ":" not in value and " " not in value


def is_port(value: Any) -> bool:
    return canonical.is_int(value) and 0 < value < 65536


def is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def is_expiry(value: Any) -> bool:
    # Up to a year: an invite is meant to be used soon.
    return canonical.is_int(value) and 0 < value <= 365 * 86400


# Every request: its op, and a validator for each of its other fields.
REQUESTS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "post": {"text": is_post_text, "images": is_image_paths},
    "feed": {"limit": is_feed_limit},
    "log": {},
    "verify": {},
    "image": {"author": is_hex64, "seq": is_seq, "index": is_image_index},
    "invite": {"host": is_host, "port": is_port, "tls": is_bool, "expires": is_expiry},
    "status": {},
    "backup": {},
}
# Ops that need the identity: to sign, to prove we're the admin, or (status) to say who we are.
NEEDS_IDENTITY = ("post", "invite", "status")
# Ops that read or write encrypted content, so they need the group key.
NEEDS_GROUP_KEY = ("post", "feed", "image", "status")
# Ops that may add an entry to our log; a running node announces new HEADS after them.
WRITES_ENTRY = ("post",)


def check_request(request: Any) -> str | None:
    """Return what's wrong with a request, or None if it's well-formed."""
    if not isinstance(request, dict) or request.get("op") not in REQUESTS:
        return "unknown request"
    expected = REQUESTS[request["op"]]
    if set(request) - {"op"} != set(expected):
        return f"{request['op']} request has the wrong fields"
    for name, is_valid in expected.items():
        if not is_valid(request[name]):
            return f"{request['op']}: invalid {name}"
    return None


async def perform(context: Context, request: Any) -> dict[str, Any]:
    """Validate and carry out one request. Raises ActionError with a user-facing reason."""
    problem = check_request(request)
    if problem is not None:
        raise ActionError(problem)
    op = request["op"]
    if op == "post":
        return post(context, request["text"], request["images"])
    if op == "feed":
        return show_feed(context, request["limit"])
    if op == "invite":
        return create_invite(context, request["host"], request["port"], request["tls"], request["expires"])
    if op == "status":
        return status(context)
    if op == "image":
        return await image(context, request["author"], request["seq"], request["index"])
    if op == "log":
        return audit.log(context.store)
    if op == "backup":
        return backup_snapshot(context)
    return verify(context)


def verify(context: Context) -> dict[str, Any]:
    """`debug verify`: every entry, plus every chunk when no node is running."""
    result = audit.verify(context.store)
    # Re-hashing every chunk can take minutes on a big home node: too long to hold up a
    # running node (and its 10-second control channel), so that part needs the node stopped.
    result["chunks"] = audit.verify_chunks(context.chunks) if context.connections is None else None
    return result


def backup_snapshot(context: Context) -> dict[str, Any]:
    """The database half of `backup`: snapshot it into the data dir (see backup.py)."""
    if context.data_dir is None:
        raise ActionError("this node has no data directory to put a backup snapshot in")
    return {"snapshot": backup.make_snapshot(context.store, context.data_dir),
            "entries": context.store.entry_count()}


def need_identity(context: Context) -> Identity:
    if context.identity is None:
        raise ActionError("this needs the node's private keys, which aren't loaded")
    return context.identity


def need_group_key(context: Context) -> bytes:
    if context.group_key is None:
        raise ActionError("this node has no group key yet: run `create-group`, or `join` with an invite link")
    return context.group_key


# --- Group setup (database half; the CLI handles the key files) ---------------------


def create_group(store: Store, identity: Identity) -> dict[str, Any]:
    """Start a new group with this identity as its admin."""
    # 32 random bytes from `secrets`: group IDs must be unguessable.
    group_id = secrets.token_hex(32)
    created = entry.create_entry(identity, group_id, None, "group_create", {"admin": identity.pubkey}, entry.now_ms())
    try:
        result = store.create_group(created.raw())
    except StoreError as error:
        raise ActionError(str(error)) from error
    if result.status != ADDED:
        raise ActionError(f"couldn't create the group: {describe(result.reason, result.status)}")
    return {"group": group_id, "admin": identity.pubkey}


def join_group(store: Store, group_id: str, admin: str) -> dict[str, Any]:
    """Record the group and admin; the admin's log then arrives by sync."""
    try:
        store.join_group(group_id, admin)
    except StoreError as error:
        raise ActionError(str(error)) from error
    return {"group": group_id, "admin": admin}


def add_member(store: Store, identity: Identity, member: str) -> dict[str, Any]:
    """The admin writes a member_add entry for `member` directly (tests; real joins use invites)."""
    if store.admin() != identity.pubkey:
        raise ActionError("only the group's admin can add members")
    if member in store.members():
        raise ActionError(f"{member[:8]} is already a member")
    added = append_own(store, identity, "member_add", {"member": member})
    return {"member": member, "seq": added.seq}


def create_invite(context: Context, host: str, port: int, tls: bool, expires: int) -> dict[str, Any]:
    """A single-use invite link (home node / admin only). `tls`: friends dial inside TLS."""
    try:
        link = invite.create_invite(context.store, need_identity(context), host, port, expires, entry.now_ms(), tls)
    except invite.InviteError as error:
        raise ActionError(str(error)) from error
    return {"link": link.text(), "expires": entry.now_ms() + expires * 1000}


def set_profile(store: Store, identity: Identity, group_key: bytes, name: str) -> dict[str, Any]:
    """Write a profile entry (your display name). The latest one wins."""
    if not feed.is_valid_name(name):
        raise ActionError(f"a name is 1 to {config.MAX_NAME_CHARS} characters")
    written = append_own(store, identity, "profile", {"box": feed.seal_profile(group_key, name)})
    return {"seq": written.seq, "name": name}


# --- Posting -------------------------------------------------------------------------


def post(context: Context, text: str, image_paths: list[str]) -> dict[str, Any]:
    """The CLI's doorway: read the image files, then post_with_images()."""
    return post_with_images(context, text, [read_image_file(Path(path)) for path in image_paths])


def post_with_images(context: Context, text: str, images: list[tuple[str, bytes]]) -> dict[str, Any]:
    """Encrypt and publish a post; `images` are (name, bytes). With images, a member's post waits in the outbox."""
    if not text and not images:
        raise ActionError("a post needs some text or at least one image")
    if len(text) > config.MAX_POST_TEXT_CHARS or len(images) > config.MAX_IMAGES_PER_POST:
        raise ActionError(f"a post has up to {config.MAX_POST_TEXT_CHARS} characters and "
                          f"{config.MAX_IMAGES_PER_POST} images")
    identity, group_key = need_identity(context), need_group_key(context)
    manifests: list[dict[str, Any]] = []
    chunk_ids: list[str] = []
    for name, data in images:
        manifest = store_image(context.chunks, group_key, name, data)
        manifests.append(manifest)
        chunk_ids.extend(manifest["chunks"])
    box = feed.seal_post(group_key, text, manifests)
    check_post_fits(context.store, identity, box)
    if manifests and not context.is_home:
        row_id = Outbox(context.store, context.chunks).add(entry.now_ms(), box, chunk_ids)
        return {"waiting": True, "outbox_id": row_id, "images": len(manifests), "chunks": len(chunk_ids)}
    written = append_own(context.store, identity, "post", {"box": box})
    return {"waiting": False, "seq": written.seq, "hash": written.compute_hash(), "images": len(manifests)}


def read_image_file(path: Path) -> tuple[str, bytes]:
    """(file name, bytes), refusing anything over 10 MiB before reading it into memory."""
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ActionError(f"can't read {path.name}: {error.strerror}") from error
    if size > config.MAX_IMAGE_BYTES:
        raise ActionError(f"{path.name} is {size / 2**20:.1f} MiB; the limit is 10 MiB")
    return path.name, path.read_bytes()


def store_image(chunks: ChunkStore, group_key: bytes, name: str, data: bytes) -> dict[str, Any]:
    """Check, split and encrypt one image; save its chunks; return its manifest."""
    try:
        manifest, pieces = split_image(group_key, data)
    except ImageError as error:
        raise ActionError(f"{name}: {error}") from error
    for chunk_id, chunk in pieces:
        chunks.save(chunk_id, chunk)
    return manifest


def check_post_fits(store: Store, identity: Identity, box: str) -> None:
    """Fail now, not after a long upload, if the encrypted post is too big for one entry."""
    group_id = store.group_id()
    if group_id is None:
        raise ActionError("this profile isn't in a group yet; run `create-group`, or `join` with an invite link")
    trial = entry.create_entry(identity, group_id, None, "post", {"box": box}, 0)
    if len(trial.raw()) > config.MAX_ENTRY_BYTES:
        raise ActionError("this post is too long once encrypted; trim the text")


def append_own(store: Store, identity: Identity, kind: str, body: dict[str, Any]) -> entry.Entry:
    """Sign the next entry in our own log and store it."""
    group_id = store.group_id()
    if group_id is None:
        raise ActionError("this profile isn't in a group yet; run `create-group`, or `join` with an invite link")
    signed = entry.create_entry(identity, group_id, store.head(identity.pubkey), kind, body, entry.now_ms())
    result = store.append_entry(signed.raw())
    if result.status != ADDED:
        raise ActionError(f"the store refused it: {describe(result.reason, result.status)}")
    return signed


# --- Reading -------------------------------------------------------------------------


def show_feed(context: Context, limit: int) -> dict[str, Any]:
    """The newest posts, decrypted, plus our own posts still waiting to upload."""
    key = need_group_key(context)
    return {"posts": feed.recent_posts(context.store, key, limit), "waiting": waiting_posts(context, False)}


def waiting_posts(context: Context, home_full: bool) -> list[dict[str, Any]]:
    """Our image posts still in the outbox, decrypted for display, oldest first."""
    key = need_group_key(context)
    waiting = []
    for queued in Outbox(context.store, context.chunks).posts():
        try:
            text = feed.open_box(key, queued.box)["text"]
        except feed.PostError:
            text = None
        waiting.append({"created": queued.created, "text": text, "chunks": len(queued.chunk_ids),
                        "uploaded": len(set(queued.acked_ids)), "home_full": home_full})
    return waiting


async def image(context: Context, author: str, seq: int, index: int) -> dict[str, Any]:
    """One image from a post, as base64 (fetched from the home node if needed)."""
    try:
        mime, data = await load_image(
            context.store, context.chunks, need_group_key(context), context.fetcher, author, seq, index
        )
    except FetchError as error:
        raise ActionError(str(error)) from error
    return {"mime": mime, "size": len(data), "data": base64.b64encode(data).decode("ascii")}


def status(context: Context) -> dict[str, Any]:
    """How this node is doing: group, members, logs, forks, outbox, storage, connections."""
    store = context.store
    names = feed.profile_names(store, need_group_key(context))
    authors = [{"author": author, "name": names.get(author), "entries": head.seq}
               for author, head in store.heads().items()]
    return {
        "group": store.group_id(), "admin": store.admin(), "me": context.identity.pubkey if context.identity else None,
        "is_home": context.is_home, "members": len(store.members()), "authors": authors,
        "forked": store.forked_authors(), "outbox": len(Outbox(store, context.chunks).posts()),
        "storage_used": context.chunks.usage_bytes(), "storage_quota": context.storage_quota,
        "connections": context.connections,
    }


def describe(reason: Reason | None, status: str) -> str:
    return reason.message if reason is not None else status

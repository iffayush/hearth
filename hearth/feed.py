"""Turning stored post entries into posts a person can read.

A post entry's body is only `{"box": ...}`. Inside the box, once decrypted
with the group key, is `{"text": str, "images": [manifest, ...]}`. The store
can't check any of that (it can't see inside), so this module does, every
time a post is opened: text up to 10,000 characters, at most 4 images, every
manifest well-formed. A post that fails is shown as unreadable, never trusted.

**Paging.** `page()` returns posts newest first, ordered by the tuple
(sort_ts, author, seq), and a cursor "<sort_ts>:<author>:<seq>" for the next
page: everything strictly older. sort_ts is fixed the moment a post is stored
(future-dated posts are clamped to their arrival time), so a post never moves
between pages, and ties never skip or repeat.

**Cache.** Decrypted contents are kept in a `FeedCache` by entry hash: a post
never changes, so the web UI's 5-second poll doesn't decrypt the same posts
over and over. Display names aren't cached; they're looked up on each read.

Joke: we read your posts so you don't have to. (We decrypt them first. We have manners.)
"""

from collections import OrderedDict
from typing import Any

from hearth import config, crypto
from hearth.chunks import is_valid_manifest
from hearth.entry import is_hex64, parse_entry
from hearth.store import Store, StoredEntry

POST_FIELDS = frozenset({"text", "images"})
PROFILE_FIELDS = frozenset({"name"})


class PostError(Exception):
    """A post that can't be shown: won't decrypt, or decrypts to something malformed."""


def seal_post(group_key: bytes, text: str, manifests: list[dict[str, Any]]) -> str:
    """The `box` for a new post."""
    return crypto.seal_box(group_key, {"text": text, "images": manifests})


def is_valid_post_content(content: Any) -> bool:
    if not isinstance(content, dict) or set(content) != POST_FIELDS:
        return False
    text, images = content["text"], content["images"]
    if not isinstance(text, str) or len(text) > config.MAX_POST_TEXT_CHARS:
        return False
    return isinstance(images, list) and len(images) <= config.MAX_IMAGES_PER_POST and all(
        is_valid_manifest(manifest) for manifest in images
    )


def open_box(group_key: bytes, box: str) -> dict[str, Any]:
    """Decrypt a post's box and check what's inside. Raises PostError."""
    try:
        content = crypto.open_box(group_key, box)
    except crypto.DecryptError as error:
        raise PostError(str(error)) from error
    if not is_valid_post_content(content):
        raise PostError("this post decrypts, but its contents are malformed")
    return content


def open_post(group_key: bytes, raw: str) -> dict[str, Any]:
    """Decrypt and check a stored post entry's content."""
    parsed = parse_entry(raw.encode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("kind") != "post":
        raise PostError("not a post")
    return open_box(group_key, parsed["body"]["box"])


def seal_profile(group_key: bytes, name: str) -> str:
    """The `box` for a profile entry: just a display name, for now."""
    return crypto.seal_box(group_key, {"name": name})


def is_valid_name(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name.strip()) and len(name) <= config.MAX_NAME_CHARS


def profile_names(store: Store, group_key: bytes) -> dict[str, str]:
    """author -> display name, from each author's latest readable profile entry."""
    names = {}
    for author, raw in store.latest_profiles().items():
        parsed = parse_entry(raw.encode("utf-8"))
        try:
            content = crypto.open_box(group_key, parsed["body"]["box"]) if isinstance(parsed, dict) else None
        except crypto.DecryptError:
            continue
        if isinstance(content, dict) and set(content) == PROFILE_FIELDS and is_valid_name(content["name"]):
            names[author] = content["name"]
    return names


class FeedCache:
    """Decrypted post contents (or why they're unreadable) by entry hash, least recently used out first."""

    def __init__(self, max_entries: int = config.FEED_CACHE_SIZE) -> None:
        self._max_entries = max_entries
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, entry_hash: str) -> dict[str, Any] | None:
        item = self._items.get(entry_hash)
        if item is not None:
            self._items.move_to_end(entry_hash)
        return item

    def put(self, entry_hash: str, item: dict[str, Any]) -> None:
        self._items[entry_hash] = item
        self._items.move_to_end(entry_hash)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)


def parse_cursor(text: str) -> tuple[int, str, int]:
    """"<sort_ts>:<author>:<seq>" -> (sort_ts, author, seq). Raises ValueError for anything else."""
    parts = text.split(":")
    if len(parts) != 3 or not parts[0].isdigit() or not parts[2].isdigit() or not is_hex64(parts[1]):
        raise ValueError("a cursor looks like <sort_ts>:<author>:<seq>")
    sort_ts, seq = int(parts[0]), int(parts[2])
    if sort_ts > config.MAX_TS or not 1 <= seq <= config.MAX_SEQ:
        raise ValueError("cursor numbers out of range")
    return sort_ts, parts[1], seq


def format_cursor(stored: StoredEntry) -> str:
    return f"{stored.sort_ts}:{stored.author}:{stored.seq}"


def page(
    store: Store, group_key: bytes, limit: int, before: tuple[int, str, int] | None = None,
    cache: FeedCache | None = None,
) -> dict[str, Any]:
    """One page of posts, newest first, plus the cursor for the next (older) page, or None."""
    names = profile_names(store, group_key)
    rows = store.posts(limit, before)
    posts = []
    for stored in rows:
        content = read_post(group_key, stored, cache)
        posts.append({"author": stored.author, "name": names.get(stored.author), "seq": stored.seq,
                      "sort_ts": stored.sort_ts, **content})
    return {"posts": posts, "next": format_cursor(rows[-1]) if len(rows) == limit else None}


def read_post(group_key: bytes, stored: StoredEntry, cache: FeedCache | None) -> dict[str, Any]:
    """{"text", "images" (how many), "problem"} for one stored post, from the cache when we can."""
    cached = cache.get(stored.hash) if cache is not None else None
    if cached is not None:
        return cached
    try:
        content = open_post(group_key, stored.raw)
        item: dict[str, Any] = {"text": content["text"], "images": len(content["images"]), "problem": None}
    except PostError as error:
        item = {"text": None, "images": 0, "problem": str(error)}
    if cache is not None:
        cache.put(stored.hash, item)
    return item


def recent_posts(store: Store, group_key: bytes, limit: int) -> list[dict[str, Any]]:
    """The newest posts, decrypted, newest first. Unreadable ones say why instead."""
    return page(store, group_key, limit)["posts"]

"""Tests for feed.py: opening posts, and refusing what's malformed inside the box."""

import pytest

from hearth import config, crypto, feed
from hearth.chunks import split_image


KEY = crypto.new_group_key()


def manifest_of(size: int = 100) -> dict[str, object]:
    return split_image(KEY, b"GIF89a" + b"\0" * (size - 6))[0]


def test_open_box_returns_text_and_manifests() -> None:
    box = feed.seal_post(KEY, "look at this", [manifest_of()])
    content = feed.open_box(KEY, box)
    assert content["text"] == "look at this" and len(content["images"]) == 1


@pytest.mark.parametrize(
    "content",
    [
        {"text": "x" * (config.MAX_POST_TEXT_CHARS + 1), "images": []},
        {"text": "five images", "images": [manifest_of()] * 5},
        {"text": "bad manifest", "images": [{"mime": "image/png"}]},
        {"text": 42, "images": []},
        {"text": "extra", "images": [], "pinned": True},
        ["not", "an", "object"],
    ],
)
def test_malformed_post_content_is_unreadable(content: object) -> None:
    # A member's node could encrypt anything; the reader checks what's inside.
    box = crypto.seal_box(KEY, content)  # type: ignore[arg-type]
    with pytest.raises(feed.PostError):
        feed.open_box(KEY, box)


# --- Cursors and the cache ------------------------------------------------------------


def test_cursor_round_trip() -> None:
    assert feed.parse_cursor(f"1700000000000:{'ab' * 32}:7") == (1700000000000, "ab" * 32, 7)


@pytest.mark.parametrize(
    "text",
    ["", "1:2", f"x:{'ab' * 32}:1", f"1:{'AB' * 32}:1", f"1:{'ab' * 32}:0", f"1:{'ab' * 32}:{2**31}",
     f"-1:{'ab' * 32}:1", f"{2**53}:{'ab' * 32}:1", f"1:{'ab' * 32}:1:extra"],
)
def test_bad_cursors_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        feed.parse_cursor(text)


def test_cache_keeps_the_most_recently_used() -> None:
    cache = feed.FeedCache(max_entries=2)
    cache.put("a", {"text": "A"})
    cache.put("b", {"text": "B"})
    assert cache.get("a") == {"text": "A"}  # a is now the most recent
    cache.put("c", {"text": "C"})  # so b is the one to go
    assert cache.get("b") is None and cache.get("a") is not None and len(cache) == 2

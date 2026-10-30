"""Tests for actions.py: request checking and the user-facing operations, run directly
(the "no node running" doorway). The running-node doorway is in test_control.py."""

import base64
from pathlib import Path
from typing import Any

import pytest

from hearth import actions, audit, config, crypto
from hearth.actions import ActionError, Context
from hearth.chunks import ChunkStore
from hearth.identity import Identity
from hearth.store import Store

from conftest import make_image


@pytest.fixture
def context(tmp_path: Path, alice: Identity) -> Context:
    """alice, admin of a fresh group, on a member node (not the home node)."""
    store = Store.open(tmp_path / "hearth.db")
    (tmp_path / "chunks").mkdir()
    created = Context(store, ChunkStore(tmp_path / "chunks", store), alice, crypto.new_group_key())
    actions.create_group(store, alice)
    yield created
    store.close()


async def run(context: Context, **request: Any) -> dict[str, Any]:
    return await actions.perform(context, request)


async def test_post_and_feed_decrypt(context: Context) -> None:
    for text in ("first", "second"):
        await run(context, op="post", text=text, images=[])
    posts = (await run(context, op="feed", limit=10))["posts"]
    assert sorted(post["text"] for post in posts) == ["first", "second"]
    assert all(post["problem"] is None and post["images"] == 0 for post in posts)


async def test_nothing_readable_is_stored(context: Context) -> None:
    await run(context, op="post", text="the secret is cardamom", images=[])
    raw = context.store.get_entry(context.identity.pubkey, 2).raw  # type: ignore[union-attr]
    assert "cardamom" not in raw and '"box"' in raw


async def test_feed_with_the_wrong_group_key_says_so(context: Context) -> None:
    await run(context, op="post", text="for members only", images=[])
    outsider = Context(context.store, context.chunks, None, crypto.new_group_key())
    post = (await run(outsider, op="feed", limit=10))["posts"][0]
    assert post["text"] is None and "wrong group key" in post["problem"]


async def test_post_needs_text_or_an_image(context: Context) -> None:
    with pytest.raises(ActionError, match="some text or at least one image"):
        await run(context, op="post", text="", images=[])


async def test_post_before_any_group_is_explained(tmp_path: Path, bob: Identity) -> None:
    store = Store.open(tmp_path / "other.db")
    lonely = Context(store, ChunkStore(tmp_path, store), bob, crypto.new_group_key())
    with pytest.raises(ActionError, match="isn't in a group"):
        await run(lonely, op="post", text="into the void", images=[])
    store.close()


async def test_post_that_is_too_long_once_encrypted(context: Context) -> None:
    # 10,000 control characters: each becomes a 6-byte \\u escape in JSON, then base64 grows it.
    with pytest.raises(ActionError, match="too long once encrypted"):
        await run(context, op="post", text="\x01" * config.MAX_POST_TEXT_CHARS, images=[])


# --- Images ------------------------------------------------------------------------


async def test_image_post_on_a_member_waits_in_the_outbox(context: Context, tmp_path: Path) -> None:
    picture = make_image(tmp_path, "cat.png", 2 * config.CHUNK_BYTES + 5)
    result = await run(context, op="post", text="my cat", images=[str(picture)])
    assert result == {"waiting": True, "outbox_id": 1, "images": 1, "chunks": 3}
    assert context.store.head(context.identity.pubkey).seq == 1  # type: ignore[union-attr]  # not in the log yet
    assert context.chunks.usage_bytes() > 2 * config.CHUNK_BYTES
    waiting = (await run(context, op="feed", limit=5))["waiting"]
    assert waiting == [{"created": waiting[0]["created"], "text": "my cat", "chunks": 3, "uploaded": 0,
                       "home_full": False}]


async def test_image_post_on_the_home_node_goes_straight_in(context: Context, tmp_path: Path) -> None:
    context.is_home = True
    picture = make_image(tmp_path, "cat.gif", 1000, kind="gif")
    result = await run(context, op="post", text="", images=[str(picture)])
    assert result["waiting"] is False and result["seq"] == 2
    image = await run(context, op="image", author=context.identity.pubkey, seq=2, index=0)  # type: ignore[union-attr]
    assert image["mime"] == "image/gif"
    assert base64.b64decode(image["data"]) == picture.read_bytes()


async def test_fake_image_is_refused(context: Context, tmp_path: Path) -> None:
    impostor = tmp_path / "definitely_a_cat.png"
    impostor.write_bytes(b"MZ" + b"\0" * 100)  # a Windows .exe, wearing a .png hat
    with pytest.raises(ActionError, match="not a JPEG, PNG, WebP or GIF"):
        await run(context, op="post", text="cute", images=[str(impostor)])


async def test_oversized_image_is_refused(context: Context, tmp_path: Path) -> None:
    huge = make_image(tmp_path, "huge.png", config.MAX_IMAGE_BYTES + 1)
    with pytest.raises(ActionError, match="the limit is 10 MiB"):
        await run(context, op="post", text="big", images=[str(huge)])


async def test_image_not_on_this_node_needs_a_running_node(context: Context, tmp_path: Path) -> None:
    context.is_home = True
    picture = make_image(tmp_path, "cat.png", 100)
    await run(context, op="post", text="", images=[str(picture)])
    for path in context.chunks.root.rglob("*"):
        if path.is_file():
            path.unlink()  # as if this node had never fetched the chunk
    with pytest.raises(ActionError, match="aren't stored on this node"):
        await run(context, op="image", author=context.identity.pubkey, seq=2, index=0)  # type: ignore[union-attr]


# --- Membership and requests -----------------------------------------------------------


def test_only_the_admin_adds_members(context: Context, bob: Identity) -> None:
    actions.add_member(context.store, context.identity, bob.pubkey)  # type: ignore[arg-type]
    with pytest.raises(ActionError, match="already a member"):
        actions.add_member(context.store, context.identity, bob.pubkey)  # type: ignore[arg-type]
    with pytest.raises(ActionError, match="only the group's admin"):
        actions.add_member(context.store, bob, Identity.generate().pubkey)


def test_join_group_only_once(tmp_path: Path, alice: Identity) -> None:
    store = Store.open(tmp_path / "joiner.db")
    actions.join_group(store, "ab" * 32, alice.pubkey)
    with pytest.raises(ActionError, match="already belongs"):
        actions.join_group(store, "cd" * 32, alice.pubkey)
    store.close()


@pytest.mark.parametrize(
    "request_, problem",
    [
        ("post", "unknown request"),
        ({"op": "post", "text": "hi"}, "wrong fields"),
        ({"op": "post", "text": "hi", "images": ["relative/cat.png"]}, "invalid images"),
        ({"op": "post", "text": "hi", "images": ["/a.png"] * 5}, "invalid images"),
        ({"op": "feed", "limit": True}, "invalid limit"),
        ({"op": "feed", "limit": 0}, "invalid limit"),
        ({"op": "image", "author": "ab" * 32, "seq": 1, "index": 4}, "invalid index"),
        ({"op": "create_group"}, "unknown request"),  # group setup is CLI-only now
    ],
)
async def test_bad_requests_are_refused(context: Context, request_: object, problem: str) -> None:
    with pytest.raises(ActionError, match=problem):
        await actions.perform(context, request_)


async def test_missing_keys_are_explained(context: Context) -> None:
    keyless = Context(context.store, context.chunks)
    with pytest.raises(ActionError, match="private keys"):
        await run(keyless, op="post", text="hi", images=[])
    with pytest.raises(ActionError, match="no group key"):
        await run(keyless, op="feed", limit=5)


async def test_log_shows_no_content_and_verify_is_clean(context: Context) -> None:
    await run(context, op="post", text="line one\nline two", images=[])
    log = audit.log(context.store)
    assert [item["kind"] for item in log["entries"]] == ["group_create", "post"]
    assert "line one" not in log["entries"][1]["summary"]
    assert "encrypted post" in log["entries"][1]["summary"]
    assert audit.verify(context.store) == {"checked": 2, "problems": []}

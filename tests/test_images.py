"""Images end to end with real nodes: outbox, upload, quota, and fetching from the home node.

The cast: `home` is the admin and the home node (it keeps every chunk);
`alice` and `bob` are members who only ever talk to home.
"""

import asyncio
import base64
from pathlib import Path
from typing import Any

from hearth import config, crypto, feed
from hearth.network import DialTarget
from hearth.node import Node

from conftest import NodeFactory, eventually, heads_of, make_image, seed_group


async def cast(make_node: NodeFactory, start_members: bool = True, **home_options: Any) -> tuple[Node, Node, Node]:
    home = await make_node("home", is_home=True, **home_options)
    dial = [DialTarget("127.0.0.1", home.listen_port, home.identity.pubkey)]
    alice = await make_node("alice", start=False, listen=None, dial=dial)
    bob = await make_node("bob", start=False, listen=None, dial=dial)
    seed_group(home, alice, bob)
    if start_members:
        await alice.start()
        await bob.start()
        await eventually(lambda: alice.home_peer() is not None and bob.home_peer() is not None)
    return home, alice, bob


async def post_image(node: Node, picture: Path, text: str = "look") -> dict[str, Any]:
    return await node.handle_control({"op": "post", "text": text, "images": [str(picture)]})


async def view_image(node: Node, author: Node, seq: int, index: int = 0) -> bytes:
    result = await node.handle_control({"op": "image", "author": author.identity.pubkey, "seq": seq, "index": index})
    return base64.b64decode(result["data"])


def latest_seq(node: Node) -> int:
    head = node.store.head(node.identity.pubkey)
    assert head is not None
    return head.seq


async def test_image_survives_its_poster_going_offline(make_node: NodeFactory, tmp_path: Path) -> None:
    home, alice, bob = await cast(make_node)
    picture = make_image(tmp_path, "sunset.png", 2 * config.CHUNK_BYTES + 99)
    assert (await post_image(alice, picture))["waiting"] is True

    # The moment the post appears on the home node, alice vanishes. (Waiting for
    # home, not just alice's own log: sync is pull-based, so an entry only leaves
    # alice's laptop once home has asked for it.)
    await eventually(lambda: home.store.head(alice.identity.pubkey) is not None)
    await alice.stop()
    assert alice.outbox.posts() == []
    assert bob.chunks.usage_bytes() == 0  # bob has none of the chunks yet

    # bob still gets the post (via home) and the image (from home's chunks).
    await eventually(lambda: bob.store.head(alice.identity.pubkey) is not None)
    assert await view_image(bob, alice, 1) == picture.read_bytes()
    # Every chunk home holds is ciphertext: no slice of the image appears in any file.
    for path in home.chunks.root.rglob("*"):
        if path.is_file():
            assert picture.read_bytes()[1000:2000] not in path.read_bytes()


async def test_post_appears_only_after_every_chunk_is_acked(make_node: NodeFactory, tmp_path: Path) -> None:
    home, alice, bob = await cast(make_node, start_members=False)
    picture = make_image(tmp_path, "cat.gif", 3 * config.CHUNK_BYTES, kind="gif")
    # alice is offline: the post waits in her outbox, invisible to everyone.
    await post_image(alice, picture, text="written on the train")
    assert alice.store.head(alice.identity.pubkey) is None
    assert len(alice.outbox.unacked_chunk_ids()) == 3

    # Back online: the outbox uploads, then the post joins her log and reaches home.
    await alice.start()
    await eventually(lambda: alice.outbox.posts() == [])
    await eventually(lambda: home.store.head(alice.identity.pubkey) is not None)
    assert len([path for path in home.chunks.root.rglob("*") if path.is_file()]) == 3
    assert feed.recent_posts(home.store, home.group_key, 5)[0]["text"] == "written on the train"


async def test_push_over_the_quota_keeps_the_post_in_the_outbox(make_node: NodeFactory, tmp_path: Path) -> None:
    home, alice, bob = await cast(make_node, storage_quota=config.CHUNK_BYTES)
    picture = make_image(tmp_path, "huge.png", 2 * config.CHUNK_BYTES + 5)
    await post_image(alice, picture)
    await eventually(alice.outbox.home_is_full)
    assert len(alice.outbox.posts()) == 1
    assert alice.store.head(alice.identity.pubkey) is None
    assert home.store.head(alice.identity.pubkey) is None
    assert home.chunks.usage_bytes() <= config.CHUNK_BYTES
    feed_view = await alice.handle_control({"op": "feed", "limit": 5})
    assert feed_view["waiting"][0]["text"] == "look"


async def test_home_node_posts_skip_the_outbox(make_node: NodeFactory, tmp_path: Path) -> None:
    home, alice, bob = await cast(make_node)
    picture = make_image(tmp_path, "announcement.jpg", 5000, kind="jpeg")
    assert (await post_image(home, picture, text="welcome!"))["waiting"] is False
    await eventually(lambda: heads_of(bob) == heads_of(home))
    assert await view_image(bob, home, latest_seq(home)) == picture.read_bytes()


async def test_simultaneous_views_share_one_transfer(make_node: NodeFactory, tmp_path: Path) -> None:
    home, alice, bob = await cast(make_node)
    picture = make_image(tmp_path, "popular.png", config.CHUNK_BYTES + 1)
    await post_image(home, picture)
    await eventually(lambda: heads_of(bob) == heads_of(home))
    first, second = await asyncio.gather(view_image(bob, home, latest_seq(home)), view_image(bob, home, latest_seq(home)))
    assert first == second == picture.read_bytes()
    assert bob.fetcher.chunk_wants_sent == 1


async def test_wrong_group_key_reads_nothing(make_node: NodeFactory, tmp_path: Path) -> None:
    home, alice, bob = await cast(make_node)
    picture = make_image(tmp_path, "secret.png", 1000)
    await post_image(home, picture, text="members only")
    await eventually(lambda: heads_of(bob) == heads_of(home))
    bob.group_key = crypto.new_group_key()  # bob somehow has the wrong key
    post = (await bob.handle_control({"op": "feed", "limit": 5}))["posts"][0]
    assert post["text"] is None and "wrong group key" in post["problem"]
    try:
        await view_image(bob, home, latest_seq(home))
    except Exception as error:  # noqa: BLE001 - any refusal will do; it must not succeed
        assert "wrong group key" in str(error)
    else:
        raise AssertionError("an image opened with the wrong group key")

"""The end-to-end convergence test: three nodes, never all online at once.

A posts while C is offline. B syncs from A. A goes offline. C comes online,
syncs from B, and gets A's post. All three end with identical heads.

B is the home node: the admin, and the one everybody can reach. (Members only
ever talk to the admin, so a brand-new member like C, who knows nobody else
yet, accepts B because B is the admin.)
"""

from hearth import audit, feed
from hearth.network import DialTarget

from conftest import NodeFactory, eventually, heads_of, seed_group


async def test_post_reaches_a_node_that_was_never_online_with_its_author(make_node: NodeFactory) -> None:
    b = await make_node("b", is_home=True)  # listens; the home node
    a = await make_node("a", start=False, listen=None, dial=[DialTarget("127.0.0.1", b.listen_port)])
    c = await make_node("c", start=False, listen=None, dial=[DialTarget("127.0.0.1", b.listen_port)])
    seed_group(b, a, c)

    # A connects and learns from B's log that it's a member; then, with C
    # offline, A posts, and B syncs the post from A.
    await a.start()
    await eventually(lambda: a.identity.pubkey in a.store.members())
    await a.handle_control({"op": "post", "text": "hello from A, who will be gone by the time C arrives", "images": []})
    await eventually(lambda: heads_of(b) == heads_of(a))

    # A goes offline.
    await a.stop()
    await eventually(lambda: not b.active_peers())

    # C comes online, syncs from B, and gets A's post.
    await c.start()
    await eventually(lambda: heads_of(c) == heads_of(b))

    assert heads_of(a) == heads_of(b) == heads_of(c)
    posts = feed.recent_posts(c.store, c.group_key, 10)
    assert [(post["author"], post["text"]) for post in posts] == [
        (a.identity.pubkey, "hello from A, who will be gone by the time C arrives")
    ]
    for node in (a, b, c):
        assert audit.verify(node.store)["problems"] == []

"""Tests for web.py: the guard (Host, token, headers), every API endpoint's validation,
stable paging, and the browser's view of two real nodes talking."""

from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest

from hearth import config, feed
from hearth.entry import create_entry, now_ms
from hearth.network import DialTarget
from hearth.node import Node
from hearth.web import SECURITY_HEADERS, WebServer, start_web

from conftest import NodeFactory, eventually, make_image, seed_group, write_post

WebClient = tuple[Node, WebServer, aiohttp.ClientSession]


async def open_web(node: Node) -> tuple[WebServer, aiohttp.ClientSession]:
    server = await start_web(node, 0)
    session = aiohttp.ClientSession(f"http://127.0.0.1:{server.port}", headers={"X-Hearth-Token": server.token})
    return server, session


@pytest.fixture
async def home_web(make_node: NodeFactory) -> AsyncIterator[WebClient]:
    home = await make_node("home", is_home=True)
    seed_group(home)
    server, session = await open_web(home)
    yield home, server, session
    await session.close()
    await server.close()


def post_at(node: Node, text: str, ts: int) -> None:
    """A post with a chosen timestamp (the test helper for future-dated posts)."""
    assert node.group_key is not None
    box = feed.seal_post(node.group_key, text, [])
    signed = create_entry(node.identity, node.store.group_id() or "", node.store.head(node.identity.pubkey),
                          "post", {"box": box}, ts)
    assert node.store.append_entry(signed.raw()).status == "added"


async def feed_texts(session: aiohttp.ClientSession, **params: Any) -> list[str]:
    async with session.get("/api/feed", params=params) as response:
        return [post["text"] for post in (await response.json())["posts"]]


# --- The guard -----------------------------------------------------------------------------


async def test_security_headers_on_every_response(home_web: WebClient) -> None:
    _, server, session = home_web
    for path in ("/", "/app.js", "/api/me", "/api/nope", "/nope"):
        async with session.get(path) as response:
            for name, value in SECURITY_HEADERS.items():
                assert response.headers[name] == value, (path, name)
    async with aiohttp.ClientSession() as anonymous:
        async with anonymous.get(f"http://127.0.0.1:{server.port}/api/me") as response:
            assert response.status == 401 and "Content-Security-Policy" in response.headers


async def test_api_needs_the_token(home_web: WebClient) -> None:
    _, server, _ = home_web
    base = f"http://127.0.0.1:{server.port}"
    async with aiohttp.ClientSession() as session:
        for headers in ({}, {"X-Hearth-Token": "guess"}, {"X-Hearth-Token": server.token[:-1]}):
            async with session.get(f"{base}/api/feed", headers=headers) as response:
                assert response.status == 401
        # The page itself loads without it: it has to, to read the token from its URL.
        async with session.get(f"{base}/") as response:
            assert response.status == 200 and "text/html" in response.headers["Content-Type"]


async def test_bad_host_header_is_refused(home_web: WebClient) -> None:
    _, server, session = home_web
    for host in ("evil.example", f"evil.example:{server.port}", f"127.0.0.1:{server.port + 1}", "127.0.0.1"):
        async with session.get("/api/me", headers={"Host": host}) as response:
            assert response.status == 403, host
    async with session.get("/api/me", headers={"Host": f"localhost:{server.port}"}) as response:
        assert response.status == 200


async def test_web_ui_only_listens_on_localhost(make_node: NodeFactory) -> None:
    node = await make_node("alice")
    with pytest.raises(ValueError, match="only ever listens on 127.0.0.1"):
        await start_web(node, 0, host="0.0.0.0")  # nosec B104 - the test checks this is refused


async def test_page_has_no_inline_script(home_web: WebClient) -> None:
    _, _, session = home_web
    async with session.get("/") as response:
        page = await response.text()
    assert "<script src=\"/app.js\"" in page and "<script>" not in page and "style=" not in page
    async with session.get("/app.js") as response:
        assert "javascript" in response.headers["Content-Type"]
        assert "innerHTML" not in await response.text()


# --- Me and profile -------------------------------------------------------------------------


async def test_me_and_profile(home_web: WebClient) -> None:
    home, _, session = home_web
    async with session.get("/api/me") as response:
        me = await response.json()
    assert me["pubkey"] == home.identity.pubkey and me["is_home"] is True and me["name"] is None
    async with session.post("/api/profile", json={"name": "Home Sweet Home"}) as response:
        assert response.status == 200
    async with session.get("/api/me") as response:
        assert (await response.json())["name"] == "Home Sweet Home"


@pytest.mark.parametrize(
    "body, status",
    [
        (b'{"name": ""}', 400),
        (b'{"name": "x", "admin": true}', 400),
        (b'["name"]', 400),
        (b'{"name": 1.5}', 400),
        (b"not json", 400),
        (b'{"name": "' + b"x" * 5000 + b'"}', 413),
    ],
)
async def test_bad_profile_requests(home_web: WebClient, body: bytes, status: int) -> None:
    _, _, session = home_web
    async with session.post("/api/profile", data=body, headers={"Content-Type": "application/json"}) as response:
        assert response.status == status


# --- Feed and paging ----------------------------------------------------------------------


async def test_paging_with_ties_never_skips_or_repeats(home_web: WebClient) -> None:
    home, _, session = home_web
    for number in range(7):
        post_at(home, f"tie {number}", ts=1_700_000_000_000)  # same sort_ts for all seven
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": "3", **({"before": cursor} if cursor else {})}
        async with session.get("/api/feed", params=params) as response:
            page = await response.json()
        seen += [post["text"] for post in page["posts"]]
        cursor = page["next"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(f"tie {number}" for number in range(7))
    assert len(seen) == len(set(seen)) == 7


async def test_future_dated_post_shows_at_arrival_and_stays_put(home_web: WebClient) -> None:
    home, _, session = home_web
    post_at(home, "yesterday", ts=now_ms() - 86_400_000)
    before_arrival = now_ms()
    post_at(home, "from the future", ts=now_ms() + 365 * 86_400_000)
    write_post(home, "written after it")
    first = await feed_texts(session)
    assert first == ["written after it", "from the future", "yesterday"]
    async with session.get("/api/feed") as response:
        future = next(post for post in (await response.json())["posts"] if post["text"] == "from the future")
    assert before_arrival <= future["sort_ts"] <= now_ms()  # clamped to when it arrived
    for _ in range(3):
        assert await feed_texts(session) == first  # and it never moves


@pytest.mark.parametrize(
    "params",
    [{"limit": "0"}, {"limit": "101"}, {"limit": "ten"}, {"limit": "-1"}, {"before": "nope"},
     {"before": f"1:{'AB' * 32}:1"}, {"before": f"1:{'ab' * 32}:0"}, {"page": "2"}],
)
async def test_bad_feed_queries(home_web: WebClient, params: dict[str, str]) -> None:
    _, _, session = home_web
    async with session.get("/api/feed", params=params) as response:
        assert response.status == 400


# --- Posts and images ------------------------------------------------------------------------


def post_form(text: str, images: list[tuple[str, bytes]]) -> aiohttp.FormData:
    # A browser's FormData is always multipart, images or not; do the same.
    form = aiohttp.FormData(default_to_multipart=True)
    form.add_field("text", text)
    for name, data in images:
        form.add_field("image", data, filename=name, content_type="application/octet-stream")
    return form


async def test_post_with_an_image_and_fetch_it_back(home_web: WebClient, tmp_path: Any) -> None:
    home, _, session = home_web
    picture = make_image(tmp_path, "cat.png", config.CHUNK_BYTES + 77).read_bytes()
    async with session.post("/api/posts", data=post_form("look", [("cat.png", picture)])) as response:
        result = await response.json()
    assert result["waiting"] is False  # the home node keeps its own chunks
    async with session.get(f"/api/image/{home.identity.pubkey}/{result['seq']}/0") as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/png"  # the verified type, not the upload's claim
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.read() == picture
    async with session.get("/api/feed") as response:
        newest = (await response.json())["posts"][0]
    assert newest["text"] == "look" and newest["images"] == 1


@pytest.mark.parametrize(
    "images, status",
    [
        ([("a.png", b"\x89PNG\r\n\x1a\n" + b"1" * 10)] * 5, 400),           # five images
        ([("big.png", b"\x89PNG\r\n\x1a\n" + b"1" * config.MAX_IMAGE_BYTES)], 413),  # over 10 MiB
        ([("cat.png", b"MZ not an image")], 400),                          # magic bytes say no
    ],
)
async def test_bad_posts(home_web: WebClient, images: list[tuple[str, bytes]], status: int) -> None:
    _, _, session = home_web
    async with session.post("/api/posts", data=post_form("hi", images)) as response:
        assert response.status == status


async def test_post_must_be_multipart_with_known_fields(home_web: WebClient) -> None:
    _, _, session = home_web
    async with session.post("/api/posts", json={"text": "hi"}) as response:
        assert response.status == 400
    form = aiohttp.FormData()
    form.add_field("text", "hi")
    form.add_field("script", "<script>")
    async with session.post("/api/posts", data=form) as response:
        assert response.status == 400


@pytest.mark.parametrize(
    "path, status",
    [
        (f"/api/image/{'xy' * 32}/1/0", 400),
        (f"/api/image/{'AB' * 32}/1/0", 400),
        (f"/api/image/{'ab' * 32}/0/0", 400),
        (f"/api/image/{'ab' * 32}/{2**31}/0", 400),
        (f"/api/image/{'ab' * 32}/one/0", 400),
        (f"/api/image/{'ab' * 32}/1/4", 400),
        (f"/api/image/{'ab' * 32}/1/-1", 400),
        (f"/api/image/{'ab' * 32}/1/0", 404),  # well-formed, but there's no such post
        ("/api/image/..%2F..%2Fhearth.db/1/0", 400),
    ],
)
async def test_image_path_parameters_are_validated(home_web: WebClient, path: str, status: int) -> None:
    _, _, session = home_web
    async with session.get(path) as response:
        assert response.status == status, await response.text()


async def test_status(home_web: WebClient) -> None:
    home, _, session = home_web
    async with session.get("/api/status") as response:
        status = await response.json()
    assert status["home_connected"] is True and status["is_home"] is True
    assert status["members"] == 1 and status["forked"] == [] and status["outbox"] == 0


# --- Two real nodes, seen through the browser API --------------------------------------------


async def test_friends_post_appears_without_refreshing(make_node: NodeFactory, tmp_path: Any) -> None:
    home = await make_node("home", is_home=True)
    bob = await make_node("bob", start=False, listen=None,
                          dial=[DialTarget("127.0.0.1", home.listen_port, home.identity.pubkey)])
    seed_group(home, bob)
    await bob.start()
    home_server, home_session = await open_web(home)
    bob_server, bob_session = await open_web(bob)
    try:
        # The owner posts from the browser...
        async with home_session.post("/api/posts", data=post_form("dinner at 8?", [])) as response:
            assert response.status == 200
        # ...and the friend's post, written on another node, shows up in the owner's next poll.
        await eventually(lambda: bob.identity.pubkey in bob.store.members())
        await bob.handle_control({"op": "post", "text": "count me in", "images": []})
        await eventually(lambda: home.store.head(bob.identity.pubkey) is not None)
        assert (await feed_texts(home_session))[:2] == ["count me in", "dinner at 8?"]

        # bob's browser shows home's image, fetched from home on demand.
        picture = make_image(tmp_path, "menu.jpg", 3000, kind="jpeg").read_bytes()
        async with home_session.post("/api/posts", data=post_form("menu", [("menu.jpg", picture)])) as response:
            seq = (await response.json())["seq"]
        await eventually(lambda: bob.store.head(home.identity.pubkey) is not None
                         and bob.store.head(home.identity.pubkey).seq >= seq)  # type: ignore[union-attr]
        async with bob_session.get(f"/api/image/{home.identity.pubkey}/{seq}/0") as response:
            assert response.status == 200 and await response.read() == picture
        async with bob_session.get("/api/status") as response:
            assert (await response.json())["home_connected"] is True
    finally:
        for session in (home_session, bob_session):
            await session.close()
        for server in (home_server, bob_server):
            await server.close()


async def test_page_files_are_revalidated_every_load(home_web: WebClient) -> None:
    # Otherwise a browser can keep running an old app.js after Hearth is updated.
    _, _, session = home_web
    for path in ("/", "/app.js", "/style.css"):
        async with session.get(path) as response:
            assert response.headers["Cache-Control"] == "no-cache", path
